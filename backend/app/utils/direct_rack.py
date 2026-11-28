"""The hotends of a direct-connected rack (H3C, P3C) and which trays each may print — Bambu Studio's rule.

On a direct rack the tray decides the PrintTool: rack slot k feeds PrintTool
k + 1, external input v feeds PrintTool 256 - v, a sub-AMS tray the hotend
whose ``bind_ams`` lists its unit. What a print must check is the hotend's
spec, not a position: the hotend the chosen tray leads to must exist and have
the diameter and flow the filament was sliced for.

Everything here is a pure reading of the printer's state, mirrored from Bambu
Studio v02.09.01.70 / v02.09.02.62:

* ``DevNozzleSystem.cpp`` — the nozzle parse (``exist`` / ``hc`` masks, the
  inlet flag from ``stat``, ``snow``), ``sTraverseBindAmsSlot`` (the one bound
  slot);
* ``DevMapping.cpp`` — ``QuerySlotFeedBlock`` (what an automatic mapping may not
  take) and the diameter / flow match;
* ``DevNozzleRack.cpp`` — ``holder.stat``, the docking-calibration bits of
  ``holder.info2``.

Tray numbers come from ``utils/tray_ids`` (the one home of the arithmetic).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from backend.app.utils.tray_ids import (
    EXTERNAL_DEPUTY_ID,
    EXTERNAL_MAIN_ID,
    ZMULTI_TYPES,
    AmsType,
    TrayLayout,
    as_int,
    bit_is_set,
    external_for_print_tool,
    split_wire_slot,
)

#: ``device.nozzle.info[].id`` of a rack dock: 0x10 + addr (``DevNozzleSystem.cpp:314-320``).
RACK_DOCK_ID_BASE = 0x10
#: ``holder.info2`` bit of dock ``addr``'s docking calibration (``IsHotendDockingCalibrated``).
_DOCK_CALI_BIT_BASE = 5
#: Rack types of ``holder.info2`` bits 1-4 (``DevNozzleRack.h:49-52``).
RACK_TYPE_A = 1  # ZMULTI_A — H3C, AMS 3H
RACK_TYPE_B = 2  # ZMULTI_B — P3C, AMS 3P
#: The rack type each rack AMS pairs with, when the holder was never seen.
_RACK_TYPE_BY_AMS_TYPE = {AmsType.ZMULTI_A: RACK_TYPE_A, AmsType.ZMULTI_B: RACK_TYPE_B}
#: Units whose trays a hotend's ``bind_ams`` may name (AMS, AMS 2 Pro, AMS-HT — ``DevMapping.cpp:243-258``).
_SUB_AMS_TYPES = frozenset({AmsType.AMS, AmsType.N3F, AmsType.N3S})

#: The flow classes a slice's ``volume_type`` names (``DevNozzle::ToNozzleFlowType``), in our vocabulary.
_FLOW_BY_VOLUME_TYPE = {
    "Standard": "standard",
    "High Flow": "high_flow",
    "TPU High Flow": "tpu_high_flow",
    "TPU285": "tpu285",
    "E3D High Flow": "e3d_high_flow",
}
#: A volume type that accepts any flow: none asked, or several flows in one slice.
_ANY_FLOW_VOLUME_TYPES = frozenset({"", "Hybrid"})
TPU285_FLOW = "tpu285"

FEED_OK = None
MANUAL_FEED = "manual_feed"
BLOCKED_BY_EXTERNAL = "blocked_by_external"
BLOCKED_BY_RACK = "blocked_by_rack"
#: A hotend's own rack slot that is not its bound one (a TPU285 hotend's, one fed
#: from its input): Studio's popup offers it as a "preload" card (``HVM:676-700``),
#: so a pin may take it; an automatic mapping never does.
PRELOAD = "preload"


@dataclass(frozen=True)
class RackHotend:
    """One rack position (``addr`` 0-based, PrintTool ``addr + 1``) as the printer reports it."""

    addr: int
    exists: bool
    is_hotend: bool = True
    diameter: float | None = None
    flow: str = ""
    #: Global tray of the entry's own ``snow``; None when it names nothing.
    slot_now: int | None = None
    #: Filament at the hotend's inlet (``stat`` bit 5, or bits 6-8 not 0).
    inlet_loaded: bool = False
    bind_ams: tuple[int, ...] = ()
    #: Docking calibration of this dock (``holder.info2`` bit 5 + addr); None while
    #: no ``holder.info2`` was seen — Studio has no direct rack without one, so its
    #: calibration checks have nothing to read and do not refuse.
    dock_calibrated: bool | None = None
    #: The tool is missing from its holder (``hc`` set, ``exist`` not): "PrintTool detached".
    detached: bool = False

    @property
    def print_tool(self) -> int:
        return self.addr + 1

    @property
    def tpu285(self) -> bool:
        return self.flow == TPU285_FLOW


@dataclass(frozen=True)
class RackState:
    """A direct rack's hotends and its holder."""

    hotends: tuple[RackHotend, ...] = ()
    #: ``holder.stat``: above 0 the rack is moving (``RACK_STATUS_IDLE`` = 0); None never seen.
    status: int | None = None
    #: 1 (A, H3C) or 2 (B, P3C): ``holder.info2`` when seen, else the rack AMS's type.
    rack_type: int | None = None
    #: The printer has reported its nozzles (``device.nozzle.info``) — before that
    #: nothing about a hotend is known, and nothing is refused for its absence.
    known: bool = False

    @property
    def busy(self) -> bool:
        return self.status is not None and self.status > 0

    def hotend(self, addr: int | None) -> RackHotend | None:
        if addr is None:
            return None
        for hotend in self.hotends:
            if hotend.addr == addr:
                return hotend
        return None

    @property
    def any_hotend(self) -> bool:
        return any(h.exists and h.is_hotend for h in self.hotends)

    @property
    def any_detached(self) -> bool:
        return any(h.detached for h in self.hotends)

    @property
    def any_dock_calibrated(self) -> bool | None:
        """Whether some dock is calibrated; None while ``holder.info2`` was never seen."""
        if any(h.dock_calibrated is None for h in self.hotends) or not self.hotends:
            return None
        return any(h.dock_calibrated for h in self.hotends)

    def facts(self) -> list:
        """What a routing snapshot's revision hashes: every fact a plan here depends on."""
        return [
            self.status,
            self.rack_type,
            self.known,
            [
                [
                    h.addr,
                    h.exists,
                    h.is_hotend,
                    h.diameter,
                    h.flow,
                    h.slot_now,
                    h.inlet_loaded,
                    list(h.bind_ams),
                    h.dock_calibrated,
                    h.detached,
                ]
                for h in self.hotends
            ],
        ]


NO_RACK = RackState()


def _is_hotend_type(type_code: str) -> bool:
    """Whether a nozzle ``type`` names a hotend, not a cutter, laser or fan (``s_parse_nozzle_type``)."""
    from backend.app.services.bambu_mqtt import _NOZZLE_FULL_NAME_MAP

    code = (type_code or "").strip()
    if code in _NOZZLE_FULL_NAME_MAP or code == "N/A":
        return True
    return len(code) >= 4 and code[0] == "H"


def hotend_flow(type_code: str) -> str:
    """The flow class of a nozzle ``type`` code (``HS00`` standard, ``HT01`` TPU285, ...), "" when unknown."""
    from backend.app.services.bambu_mqtt import _parse_nozzle_type

    return _parse_nozzle_type(type_code)[1]


def _diameter_of(entry: Mapping) -> float | None:
    """The entry's ``diameter``, else the one a long type code carries (``HS00-0.4``, rounded as BS does)."""
    try:
        diameter = float(entry.get("diameter"))
        if 0 < diameter < 3:
            return diameter
    except (TypeError, ValueError):
        pass
    try:
        diameter = float(str(entry.get("type") or "").partition("-")[2])
    except ValueError:
        return None
    return round(diameter * 10) / 10 if 0 < diameter < 3 else None


def inlet_loaded(stat: object) -> bool:
    """Filament at a direct-rack hotend's inlet: ``stat`` bit 5, or bits 6-8 not 0 (``DevNozzleSystem.cpp:1196-1204``)."""
    value = as_int(stat)
    if value is None:
        return False
    return bool((value >> 5) & 1) or ((value >> 6) & 0x7) != 0


def slot_now_of(snow: object, layout: TrayLayout) -> int | None:
    """Global tray of a nozzle entry's ``snow``; None when its low byte is 0xFF ("nothing") or it names no tray."""
    split = split_wire_slot(snow)
    if split is None or split[1] == 0xFF:
        return None
    return layout.decode_wire_slot(snow)


def rack_type_of(rack_type: int | None, layout: TrayLayout) -> int | None:
    """The holder's rack type when seen (above 0), else the rack AMS's (BS pairs ZMULTI_A with A, ZMULTI_B with B)."""
    if isinstance(rack_type, int) and rack_type > 0:
        return rack_type
    listed = layout.rack_unit
    return _RACK_TYPE_BY_AMS_TYPE.get(listed[1]) if listed is not None else None


def build_rack_state(
    nozzle_entries: Iterable[object] | None,
    layout: TrayLayout,
    *,
    exist_mask: int | None = None,
    hc_mask: int | None = None,
    holder_status: int | None = None,
    holder_info2: int | None = None,
    rack_type: int | None = None,
    units: Iterable[int] = (),
) -> RackState:
    """The rack's hotends from ``device.nozzle.info`` (raw entries or ``PrinterState.nozzle_rack``) and the holder.

    A position's entry is the one whose ``addr_id`` names it (the hotend on the
    toolhead reports ``id 0`` with its ``addr_id``), else the dock entry
    ``0x10 + addr``. ``exists`` follows the ``exist`` mask indexed by the
    entry's full id when the printer sent one, else the entry's presence (BS's
    default). Positions are 0 .. rack size - 1; one without an entry has no
    hotend. ``bind_ams`` keeps only units the printer lists.
    """
    if not layout.direct_rack or layout.rack_size <= 0:
        return NO_RACK
    entries = [e for e in nozzle_entries or () if isinstance(e, Mapping)]
    listed_units = set(units)
    hotends: list[RackHotend] = []
    for addr in range(layout.rack_size):
        entry = next((e for e in entries if as_int(e.get("addr_id")) == addr), None)
        if entry is None:
            entry = next((e for e in entries if as_int(e.get("id")) == RACK_DOCK_ID_BASE + addr), None)
        dock_calibrated = None if holder_info2 is None else bit_is_set(holder_info2, _DOCK_CALI_BIT_BASE + addr)
        if entry is None:
            hotends.append(RackHotend(addr=addr, exists=False, dock_calibrated=dock_calibrated))
            continue
        full_id = as_int(entry.get("id"))
        exists = True if exist_mask is None or full_id is None else bit_is_set(exist_mask, full_id)
        connected = True if hc_mask is None else bit_is_set(hc_mask, addr)
        type_code = str(entry.get("type") or "")
        bind = entry.get("bind_ams")
        hotends.append(
            RackHotend(
                addr=addr,
                exists=exists,
                is_hotend=_is_hotend_type(type_code),
                diameter=_diameter_of(entry),
                flow=hotend_flow(type_code),
                slot_now=slot_now_of(entry.get("snow"), layout),
                inlet_loaded=inlet_loaded(entry.get("stat")),
                bind_ams=tuple(
                    u for u in (as_int(v) for v in bind) if u is not None and (not listed_units or u in listed_units)
                )
                if isinstance(bind, (list, tuple))
                else (),
                dock_calibrated=dock_calibrated,
                detached=connected and not exists,
            )
        )
    return RackState(
        hotends=tuple(hotends),
        status=holder_status,
        rack_type=rack_type_of(rack_type, layout),
        known=bool(entries),
    )


# --- which trays a hotend may print -------------------------------------------


def bound_slot(
    hotend: RackHotend,
    layout: TrayLayout,
    trays: set[int],
    externals: set[int],
    rack_type: int | None,
) -> int | None:
    """The one tray bound to ``hotend`` (BS ``sTraverseBindAmsSlot``), or None.

    1. The entry's ``snow`` slot, when it is a hotend and the tray exists — a
       ``bind_ams`` sub-AMS tray only with filament at the inlet.
    2. The rack AMS's slot ``addr`` when that tray exists (even empty) — not
       for a TPU285 hotend on a type-A rack (BS62 narrowed BS70's "any rack").
    3. The external input ``255 - addr`` when it exists.

    ``trays`` are the global ids of the tray objects the printer lists (loaded
    or not), ``externals`` its external inputs' ids.
    """
    now = hotend.slot_now
    if now is not None and hotend.is_hotend and (now in trays or now in externals):
        unit = layout.slot_key(now)[0]
        if unit not in hotend.bind_ams or hotend.inlet_loaded:
            return now
    rack = layout.rack_unit
    if rack is not None and not (hotend.tpu285 and rack_type == RACK_TYPE_A):
        tray = layout.global_tray_id(rack[0], hotend.addr)
        if tray in trays:
            return tray
    external = external_for_print_tool(hotend.print_tool)
    if external in externals:
        return external
    return None


def bind_ams_trays(hotend: RackHotend, layout: TrayLayout, unit_trays: Mapping[int, Iterable[int]]) -> list[int]:
    """Every tray of the sub-AMS units ``hotend``'s ``bind_ams`` names (AMS, AMS 2 Pro, AMS-HT only)."""
    out: list[int] = []
    for uid in hotend.bind_ams:
        if layout.ams_type(uid) not in _SUB_AMS_TYPES:
            continue
        out.extend(unit_trays.get(uid, ()))
    return out


def candidate_print_tools(
    rack: RackState,
    layout: TrayLayout,
    trays: set[int],
    externals: set[int],
    unit_trays: Mapping[int, Iterable[int]],
) -> dict[int, int]:
    """Global tray -> the addr of the hotend it may print through, for every existing hotend.

    A tray reachable from two hotends keeps the first (BS adds a shared slot once).
    """
    out: dict[int, int] = {}
    for hotend in rack.hotends:
        if not hotend.exists:
            continue
        bound = bound_slot(hotend, layout, trays, externals, rack.rack_type)
        for tray in ([bound] if bound is not None else []) + bind_ams_trays(hotend, layout, unit_trays):
            out.setdefault(tray, hotend.addr)
    return out


def can_auto_feed(ams_type: AmsType | None, is_external: bool) -> bool:
    """BS ``CanAutoFeed``: an external input and an AMS 3P (ZMULTI_B) slot are fed by hand."""
    return not is_external and ams_type != AmsType.ZMULTI_B


def can_auto_retract(ams_type: AmsType | None, is_external: bool) -> bool:
    """BS ``CanAutoRetract``: only a sub-AMS unloads its own; an external input and a rack AMS do not."""
    return not is_external and ams_type not in ZMULTI_TYPES


def feed_block(hotend: RackHotend, tray: int, layout: TrayLayout) -> str | None:
    """BS ``QuerySlotFeedBlock``: None when an automatic mapping may take ``tray`` for ``hotend``.

    The occupant (filament at the inlet, ``snow`` naming this tray) needs
    nothing. Otherwise filament from an external input or the rack AMS in the
    hotend blocks every other tray (only a sub-AMS retracts its own); a tray
    nothing feeds by itself (an external input, an AMS 3P slot) needs a hand.
    """
    is_external = layout.is_external(tray)
    if hotend.inlet_loaded and hotend.slot_now == tray:
        return FEED_OK
    if hotend.inlet_loaded and hotend.slot_now is not None:
        occupant_external = layout.is_external(hotend.slot_now)
        occupant_type = None if occupant_external else layout.ams_type(layout.slot_key(hotend.slot_now)[0])
        if not can_auto_retract(occupant_type, occupant_external):
            return BLOCKED_BY_EXTERNAL if occupant_external else BLOCKED_BY_RACK
    tray_type = None if is_external else layout.ams_type(layout.slot_key(tray)[0])
    if not can_auto_feed(tray_type, is_external):
        return MANUAL_FEED
    return FEED_OK


def retracts_itself(tray: int, layout: TrayLayout) -> bool:
    """Whether ``tray`` unloads its own filament (a sub-AMS tray) — two such trays may share a PrintTool silently."""
    if layout.is_external(tray):
        return False
    return can_auto_retract(layout.ams_type(layout.slot_key(tray)[0]), False)


# --- what goes on the wire --------------------------------------------------

#: An unused filament in ``ams_mapping2`` (Studio's ``{255, 255}``).
_UNUSED_WIRE = {"ams_id": 255, "slot_id": 255}


def project_file_entry(tray: int, layout: TrayLayout, *, raw_externals: bool = False) -> tuple[int, dict]:
    """One filament of a direct rack's ``project_file``: ``(flat ams_mapping value, ams_mapping2 entry)``.

    Bambu Studio (``SelectMachine.cpp:1694``, ``CalibUtils.cpp:2465``):

    * a rack AMS slot k: flat ``28 + k`` / ``37 + k``, ``{32 | 33, k}``;
    * an external input 247-253: flat raw, ``{v, 0}``; 254 / 255: flat ``-1``,
      ``{v, 0}`` — Studio rewrites only those two (upstream's ``-1`` for every
      input is rejected) — except a calibration print, which sends its tray raw
      (``raw_externals``);
    * a sub-AMS tray: flat as stored, ``{unit, slot}`` (an AMS-HT ``{id, 0}``);
    * unused (``-1``): ``-1``, ``{255, 255}``.
    """
    if tray < 0:
        return -1, dict(_UNUSED_WIRE)
    if layout.is_external(tray):
        flat = tray if raw_externals or tray not in (EXTERNAL_DEPUTY_ID, EXTERNAL_MAIN_ID) else -1
        return flat, {"ams_id": tray, "slot_id": 0}
    ams_id, slot = layout.slot_key(tray)
    return tray, {"ams_id": ams_id, "slot_id": slot}


def filament_setting_ids(ams_id: int, tray_id: int, layout: TrayLayout) -> tuple[int, int, int] | None:
    """``(ams_id, tray_id, slot_id)`` of an ``ams_filament_setting`` for a direct rack's slot, else None.

    Bambu Studio ``command_ams_filament_settings`` (``DeviceManager.cpp:1801-1838``):
    a rack AMS slot k is ``{ams_id 32 | 33, slot_id k, tray_id k}``; an external
    input v (stored ``(255, idx)``) is ``{ams_id v, slot_id 0}`` with ``tray_id``
    254 for v 254 / 255 (``VIRTUAL_TRAY_DEPUTY_ID``) and 0 for 247-253. None for
    every other slot — the caller keeps its own numbers.
    """
    if not layout.direct_rack:
        return None
    if ams_id == EXTERNAL_MAIN_ID:
        external = layout.global_tray_id(ams_id, tray_id)
        if not layout.external_in_rack(external):
            return None
        return external, (EXTERNAL_DEPUTY_ID if external in (EXTERNAL_DEPUTY_ID, EXTERNAL_MAIN_ID) else 0), 0
    if layout.ams_type(ams_id) in ZMULTI_TYPES:
        return ams_id, tray_id, tray_id
    return None


# --- the slice's demand -----------------------------------------------------


def flow_of_volume_type(volume_type: str | None) -> str | None:
    """The flow class a slice's ``volume_type`` asks for; None when any flow will do (empty, "Hybrid")."""
    name = (volume_type or "").strip()
    if name in _ANY_FLOW_VOLUME_TYPES:
        return None
    return _FLOW_BY_VOLUME_TYPE.get(name, f"unknown:{name}")


def hotend_matches(hotend: RackHotend, diameter: float | None, volume_type: str | None) -> bool:
    """BS's ``Require`` match: the hotend's diameter (±1e-3) and flow are the slice's.

    No diameter asked, or none reported, compares nothing (BS needs both). An
    unknown volume type still mismatches (BS: "an unknown flow still mismatches").
    """
    return hotend_spec_matches(hotend, diameter, flow_of_volume_type(volume_type))


def hotend_spec_matches(hotend: RackHotend, diameter: float | None, flow: str | None) -> bool:
    """``hotend_matches`` with the flow in our vocabulary (``standard``, ``high_flow``, ...); None any flow."""
    if not hotend.exists or not hotend.is_hotend:
        return False
    if diameter and hotend.diameter is not None and abs(float(diameter) - hotend.diameter) >= 1e-3:
        return False
    return flow is None or flow == "hybrid" or flow == hotend.flow
