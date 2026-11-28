"""Generation-scoped filament telemetry and immutable routing snapshots.

This cache deliberately starts empty on reconnect. Shared raw_data is retained
for the UI; a temperature or remain-only update cannot refresh its old spools.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from backend.app.services.ams_advertised_overlay import matches_live, slot_key
from backend.app.utils.direct_rack import NO_RACK, PRELOAD, RackState, candidate_print_tools, feed_block
from backend.app.utils.filament_types import routing_filament_type
from backend.app.utils.model_compatibility import effective_model_for_state
from backend.app.utils.printer_models import is_dual_nozzle_model, is_nozzle_rack_model, normalize_model_name
from backend.app.utils.tray_ids import (
    NO_LAYOUT,
    TrayLayout,
    ams_exist_bit,
    ams_type_of,
    as_layout,
    is_zmulti,
    unit_extruder,
    unit_type,
)


@dataclass(frozen=True)
class FeedSource:
    id: int
    kind: str
    material: str
    color: str | None = None
    variant: str | None = None
    nozzles: tuple[int, ...] = ()
    remain: float = -1
    identity: str = ""
    #: What BamDude told the printer this slot holds (backup-compatibility
    #: emulation), set only while that advertisement is APPLIED
    #: (``ams_advertised_overlay.matches_live``). ``color`` / ``variant`` stay the
    #: SPOOL. The routing rule reads these for the forced-colour and the
    #: strict-profile checks and for colour ranking — the operator's own
    #: declaration outranks the job's exact checks (owner, 2026-09-30). Base
    #: material is always the spool's.
    declared_color: str | None = None
    declared_variant: str | None = None
    reported_material: str | None = field(default=None, compare=False)
    #: A direct rack (H3C, P3C): the hotend this source prints through, by its
    #: 0-based dock ``addr`` (PrintTool ``addr + 1``) — Bambu Studio's candidate
    #: rule (``utils/direct_rack``); None when no existing hotend is bound to it.
    #: ``feed_block``: why an automatic plan may not take it (``direct_rack.feed_block``).
    rack_addr: int | None = None
    feed_block: str | None = None

    @property
    def rule_color(self) -> str | None:
        return self.declared_color or self.color

    @property
    def rule_variant(self) -> str | None:
        return self.declared_variant or self.variant


@dataclass(frozen=True)
class PrinterFeedSnapshot:
    printer_id: int
    model: str | None
    connected: bool
    generation: int
    revision: str
    ams_known: bool
    ams_present: bool
    external_known: bool
    sources: tuple[FeedSource, ...]
    nozzle_diameters: dict[int, tuple[float, ...]] = field(default_factory=dict)
    fts: bool = False
    backup_enabled: bool | None = None
    incomplete: bool = False
    # ``fun2`` bit 7 on models whose mirrored config asks for it.  None means
    # this connection has not supplied the capability yet, not "firmware says no".
    left_tpu_firmware: bool | None = None
    fts_pending_confirmation: bool = False
    #: Which AMS slots the firmware would substitute for one another (AMS
    #: Backup): global slot id → every member of its group, itself included.
    #: The group the firmware reports now wins; a slot it no longer groups —
    #: one that ran dry — keeps the group it was last in while loaded
    #: (``backup_group_memory``). Read only for a pinned slot that is empty.
    backup_membership: dict[int, tuple[int, ...]] = field(default_factory=dict)
    #: A direct rack's hotends and holder; ``NO_RACK`` on every other printer.
    rack: RackState = NO_RACK
    #: How this printer numbers its trays (labels, externals, rack slots).
    layout: TrayLayout = NO_LAYOUT

    @property
    def marker(self) -> tuple[int, str]:
        return self.generation, self.revision

    @property
    def direct_rack(self) -> bool:
        return self.layout.direct_rack


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# What the firmware puts in a hotend's serial when no nozzle is mounted on it.
_EMPTY_NOZZLE_SERIAL = "N/A"


def _hotend_states_empty(entry: dict) -> bool:
    """A hotend entry of ``device.nozzle.info`` that SAYS nothing is mounted.

    A hotend that parked its nozzle back in the rack keeps reporting that
    nozzle's diameter — measured on an H2C at idle: diameter "0.4", serial
    "N/A", no temperature rating (upstream #2885). The diameter is therefore no
    sign of presence. Emptiness has to be stated: the serial must be the
    firmware's "N/A" AND the rating absent; a firmware that reports neither has
    said nothing, and reading that as empty would switch the nozzle check off.
    Rack docks (ids 16+) are not asked — an empty dock is absent altogether.
    """
    serial = str(entry.get("serial_number") or entry.get("sn") or "").strip().upper()
    if serial != _EMPTY_NOZZLE_SERIAL:
        return False
    try:
        return float(entry.get("max_temp") or entry.get("tm") or 0) <= 0
    except (TypeError, ValueError):
        return False


@dataclass
class FeedTelemetry:
    ams_known: bool = False
    ams_present: bool = False
    external_known: bool = False
    units: dict[int, dict] = field(default_factory=dict)
    external: dict[int, dict] = field(default_factory=dict)
    nozzles: dict[int, set[float]] = field(default_factory=dict)
    fts: bool = False
    fts_authoritative: bool = False
    left_tpu_firmware: bool | None = None
    #: The printer's tray numbering the last frame was read with (``utils/tray_ids``).
    layout: TrayLayout = NO_LAYOUT

    def observe(self, payload: dict, model: str | None, layout: TrayLayout | None = None) -> None:
        layout = as_layout(layout)
        self.layout = layout
        report = payload.get("print", {})
        if not isinstance(report, dict):
            return
        # Calibration replies describe a queried diameter, not installed hardware.
        if report.get("command") not in (None, "push_status"):
            report = {}
        # FTS installation is authoritative in aux bit 29.  Some older
        # firmware emits only the detail object; retain it as positive fallback
        # until an aux value has appeared in this connection generation.
        if "aux" in report:
            try:
                aux = report["aux"] if isinstance(report["aux"], int) else int(str(report["aux"]), 16)
            except (TypeError, ValueError):
                aux = None
            if aux is not None:
                self.fts_authoritative = True
                self.fts = bool((aux >> 29) & 1)
        if "fun2" in report:
            try:
                fun2 = report["fun2"] if isinstance(report["fun2"], int) else int(str(report["fun2"]), 16)
            except (TypeError, ValueError):
                fun2 = None
            if fun2 is not None:
                self.left_tpu_firmware = bool((fun2 >> 7) & 1)
        ams = report.get("ams", payload.get("ams"))
        if isinstance(ams, (dict, list)):
            envelope = ams if isinstance(ams, dict) else {"ams": ams}
            units = envelope.get("ams")
            bits = envelope.get("ams_exist_bits")
            if bits is not None:
                try:
                    mask = int(bits, 16) if isinstance(bits, str) else int(bits)
                    self.ams_known = True
                    self.ams_present = bool(mask)
                    if mask == 0:
                        self.units.clear()
                except (TypeError, ValueError):
                    pass
            if isinstance(units, list):
                self.ams_known = True
                if bits is None:
                    self.ams_present = bool(units)
                if not units:
                    self.units.clear()
                for unit in units:
                    if not isinstance(unit, dict):
                        continue
                    from backend.app.services.bambu_mqtt import normalize_am_unit_id

                    wire_uid = _integer(unit.get("id"))
                    uid = normalize_am_unit_id(wire_uid) if wire_uid is not None else None
                    # A rack AMS (AMS 3H unit 32, AMS 3P unit 33): told by its ``info`` type,
                    # else by its id, as the printer's own merge does (``tray_ids``).
                    typed = unit_type(unit)
                    zmulti = typed is not None and is_zmulti(typed[1])
                    if uid is None or not (0 <= uid < 16 or 128 <= uid <= 143 or zmulti):
                        continue
                    current = self.units.setdefault(uid, {"id": uid, "tray": []})
                    current["_wire_id"] = wire_uid
                    if zmulti:
                        current["_zmulti"] = True
                    zmulti = zmulti or current.get("_zmulti", False)
                    trays = {str(t.get("id")): dict(t) for t in current["tray"]}
                    if unit.get("tray") == []:
                        trays.clear()
                    for tray in unit.get("tray", []) or []:
                        if not isinstance(tray, dict):
                            continue
                        tray_id = _integer(tray.get("id"))
                        last_slot = layout.slot_limit(uid) - 1 if zmulti else (0 if uid >= 128 else 3)
                        if tray_id is None or not 0 <= tray_id <= last_slot:
                            continue
                        tid = str(tray_id)
                        merged = {**trays.get(tid, {}), **tray}
                        # The "not 11 -> empty" reading is a regular AMS's; a rack AMS's
                        # slot states mean otherwise (stage A, ``tray_ids``).
                        if tray.get("tray_type") == "" or (
                            uid < 128
                            and not zmulti
                            and "state" in tray
                            and tray["state"] != 11
                            and "tray_type" not in tray
                        ):
                            merged = {"id": tray.get("id"), "tray_type": ""}
                        trays[tid] = merged
                    current.update({k: v for k, v in unit.items() if k != "tray"})
                    if "tray" in unit:
                        current["_trays_seen"] = True
                    current["tray"] = list(trays.values())
            if bits is not None:
                try:
                    mask = int(bits, 16) if isinstance(bits, str) else int(bits)
                    if mask == 0:
                        self.units.clear()
                    else:
                        # Regular AMS IDs are bit positions. HT units have a
                        # separate protocol range; do not infer their bit layout.
                        # A rack AMS has its own bit (13 / 14, ``tray_ids.ams_exist_bit``).
                        for uid in list(self.units):
                            if self.units[uid].get("_zmulti"):
                                bit = ams_exist_bit(uid, ams_type_of(uid))
                                if bit is not None and not mask & (1 << bit):
                                    del self.units[uid]
                            elif uid < 16 and not mask & (1 << self.units[uid].get("_wire_id", uid)):
                                del self.units[uid]
                except (TypeError, ValueError):
                    pass
            if "tray_exist_bits" in envelope:
                from backend.app.services.bambu_mqtt import apply_tray_exist_bits

                apply_tray_exist_bits(
                    list(self.units.values()),
                    envelope["tray_exist_bits"],
                    power_on_flag=envelope.get("power_on_flag", True),
                    ams_exist_bits=bits,
                )
        dual = is_dual_nozzle_model(normalize_model_name(model))
        # vir_slot is the authoritative dual shape; vt_tray can be a one-tray echo.
        external = report.get("vir_slot", report.get("vt_tray"))
        if isinstance(external, dict):
            external = [external]
        if isinstance(external, list):
            self.external_known = True
            if not external:
                self.external.clear()
            for tray in external:
                if not isinstance(tray, dict):
                    continue
                if layout.direct_rack:
                    # Each PrintTool has its own input, named by its id (255 PrintTool 1 ...
                    # 247 PrintTool 9); never folded into one 254.
                    tid = _integer(tray.get("id"))
                    if tid is None or not layout.external_in_rack(tid):
                        continue
                else:
                    tid = _integer(tray.get("id"), 254) if dual else 254
                    if tid not in (254, 255):
                        continue
                merged = {**self.external.get(tid, {}), **tray, "id": tid}
                if tray.get("tray_type") == "":
                    merged = {"id": tid, "tray_type": ""}
                self.external[tid] = merged
        device = report.get("device") or {}
        if isinstance(device, dict):
            if isinstance(device.get("fila_switch"), dict) and not self.fts_authoritative:
                self.fts = True
            nozzle = device.get("nozzle") or {}
            if isinstance(nozzle, dict) and isinstance(nozzle.get("info"), list):
                # A full info list replaces the installed/rack set, including empty.
                self.nozzles.clear()
                for entry in nozzle["info"]:
                    if not isinstance(entry, dict):
                        continue
                    nid = _integer(entry.get("id"))
                    if nid is None:
                        continue
                    # Only an H2C-style rack can leave a real hotend empty.
                    # Fixed H2D hotends report ``sn=N/A`` and ``tm=0`` even
                    # while their nozzles are installed, so the rack-specific
                    # absence marker must not hide them from routing.
                    if nid < 16 and is_nozzle_rack_model(model) and _hotend_states_empty(entry):
                        continue
                    # ids 0/1 are the hotends on MQTT extruders 0/1 on every H2,
                    # the H2C included; only a dock needs placing — on the
                    # carriage that fetches from it (upstream 45dc139c).
                    if nid >= 16 and is_nozzle_rack_model(model):
                        from backend.app.utils.printer_models import NOZZLE_RACK_EXTRUDER_INDEX

                        nid = NOZZLE_RACK_EXTRUDER_INDEX
                    self._diameter(nid, entry.get("diameter"))
        for key, nid in (
            ("nozzle_diameter", 0),
            ("left_nozzle_diameter", 1),
            ("right_nozzle_diameter", 0),
            ("nozzle_diameter_2", 1),
        ):
            if key in report:
                self.nozzles.pop(nid, None)
                self._diameter(nid, report[key])

    def loaded_source_ids(self) -> set[int]:
        """Global ids of the AMS slots whose tray reports a filament type now."""
        ids: set[int] = set()
        for uid, unit in self.units.items():
            for tray in unit.get("tray", []):
                tid = _integer(tray.get("id"))
                if tid is not None and tray.get("tray_type"):
                    ids.add(self.layout.global_tray_id(uid, tid))
        return ids

    def tray_ids(self) -> set[int]:
        """Global ids of every AMS tray the printer lists, loaded or not (a direct rack's bound-slot rule)."""
        ids: set[int] = set()
        for uid, unit in self.units.items():
            for tray in unit.get("tray", []):
                tid = _integer(tray.get("id"))
                if tid is not None:
                    ids.add(self.layout.global_tray_id(uid, tid))
        return ids

    def unit_tray_ids(self) -> dict[int, list[int]]:
        """Each unit's listed trays as global ids (a hotend's ``bind_ams`` sub-AMS candidates)."""
        out: dict[int, list[int]] = {}
        for uid, unit in self.units.items():
            for tray in unit.get("tray", []):
                tid = _integer(tray.get("id"))
                if tid is not None:
                    out.setdefault(uid, []).append(self.layout.global_tray_id(uid, tid))
        return out

    def _diameter(self, nozzle_id, value):
        try:
            diameter = float(value)
            if 0 < diameter < 3:
                self.nozzles.setdefault(nozzle_id, set()).add(diameter)
        except (TypeError, ValueError):
            pass


def snapshot_from_state(
    printer_id: int, model: str | None, state, overlay=None, remembered_groups=None
) -> PrinterFeedSnapshot:
    normalized = normalize_model_name(model)
    routing_model = effective_model_for_state(model, state)
    dual = is_dual_nozzle_model(normalized)
    telemetry = getattr(state, "feed_telemetry", None)
    if not isinstance(telemetry, FeedTelemetry):
        telemetry = FeedTelemetry()
    layout = TrayLayout.from_state(state, model) if state is not None else NO_LAYOUT
    rack = _rack_of(state, layout)
    # A direct rack: which hotend each tray prints through (Studio's candidate rule) —
    # computed over every listed tray, loaded or not (a bound rack slot may be empty).
    rack_bind = (
        candidate_print_tools(rack, layout, telemetry.tray_ids(), set(telemetry.external), telemetry.unit_tray_ids())
        if layout.direct_rack
        else {}
    )
    sources = []
    # What the overlay actually changed, folded into the revision below: a slot
    # whose advertised profile is masked must invalidate every cached routing
    # decision taken while the live values were the only thing we knew.
    applied_overlay: list = []

    def add(tray, sid, kind, nozzles, key=None):
        material = tray.get("tray_type")
        if not material:
            return
        try:
            remain = float(tray.get("remain", -1))
        except (TypeError, ValueError):
            remain = -1
        color, variant = tray.get("tray_color"), tray.get("tray_info_idx")
        declared_color = declared_variant = None
        entry = overlay.get(key) if overlay and key is not None else None
        if entry is not None and matches_live(entry, tray):
            material, color, variant = entry.actual_material, entry.actual_color, entry.actual_variant
            declared_color, declared_variant = entry.advertised_color or None, entry.advertised_variant or None
            applied_overlay.append([key[0], key[1], material, color, variant])
        rack_addr = block = None
        if layout.direct_rack:
            # Studio takes a tray only when its type AND colour are known (``is_tray_info_ready``).
            if not color:
                return
            rack_addr = rack_bind.get(sid)
            hotend = rack.hotend(rack_addr)
            block = feed_block(hotend, sid, layout) if hotend is not None else None
            own_slot = layout.rack_slot(sid) if rack_addr is None else None
            if own_slot is not None and (own := rack.hotend(own_slot[1])) is not None and own.exists:
                # Its hotend's own rack slot, not the bound one: a pin may take it (Studio's preload card).
                rack_addr, block = own_slot[1], PRELOAD
        sources.append(
            FeedSource(
                id=sid,
                kind=kind,
                material=routing_filament_type(material, variant),
                color=color,
                variant=variant,
                nozzles=nozzles,
                remain=remain,
                identity=str(tray.get("tray_uuid") or tray.get("tag_uid") or ""),
                declared_color=declared_color,
                declared_variant=declared_variant,
                reported_material=tray.get("tray_type"),
                rack_addr=rack_addr,
                feed_block=block,
            )
        )

    for uid, unit in telemetry.units.items():
        extruder = unit_extruder(unit["info"]) if "info" in unit else None
        nozzles = (0, 1) if dual and telemetry.fts else ((extruder,) if extruder in (0, 1) else ())
        if not dual:
            nozzles = (0,)
        for tray in unit.get("tray", []):
            tid = _integer(tray.get("id"))
            if tid is not None:
                # The overlay is keyed by (unit, slot) as the assignment rows
                # are, NOT by the global source id this snapshot sorts on —
                # ``slot_key`` owns the HT normalisation for every caller.
                add(tray, layout.global_tray_id(uid, tid), "ams", nozzles, key=slot_key(uid, tid))
    for tid, tray in telemetry.external.items():
        if layout.direct_rack:
            # One toolhead: every input is on extruder 0, its PrintTool is its own.
            add(tray, tid, "external", (0,))
            continue
        nozzle = (255 - tid) if dual else 0
        add(tray, tid, "external", (nozzle,) if nozzle in (0, 1) else ())
    sources.sort(key=lambda s: s.id)
    incomplete = (
        (telemetry.ams_present and not telemetry.units)
        or any("tray_type" not in t for t in telemetry.external.values())
        or any(
            not u.get("_trays_seen") or any("tray_type" not in t for t in u["tray"]) for u in telemetry.units.values()
        )
    )
    diameters = {k: tuple(sorted(v)) for k, v in telemetry.nozzles.items()}
    if layout.direct_rack:
        # One extruder (0) whose hotends are the rack's: the diameters any existing
        # hotend has (BS ``CollectNozzles`` over the toolhead and the rack). Which
        # hotend a source reaches is the source's ``rack_addr``.
        rack_diameters = tuple(sorted({h.diameter for h in rack.hotends if h.exists and h.is_hotend and h.diameter}))
        diameters = {0: rack_diameters} if rack_diameters else {}
    membership: dict[int, tuple[int, ...]] = {
        member: tuple(sorted(members)) for member, members in (remembered_groups or {}).items()
    }
    for extruder_groups in (getattr(state, "ams_backup_groups", None) or {}).values():
        for group in extruder_groups:
            members = tuple(sorted(set(group)))
            if len(members) >= 2:
                for member in members:
                    membership[member] = members
    payload = {
        "model": routing_model,
        "ams_known": telemetry.ams_known,
        "ams_present": telemetry.ams_present,
        "external_known": telemetry.external_known,
        "nozzles": diameters,
        "fts": telemetry.fts,
        "fts_pending_confirmation": getattr(state, "fts_pending_confirmation", False) is True,
        "left_tpu_firmware": telemetry.left_tpu_firmware,
        "incomplete": incomplete,
        "sources": [{k: v for k, v in asdict(s).items() if k not in {"remain", "reported_material"}} for s in sources],
        # Sorted, because these are collected in ``telemetry.units`` insertion
        # order: a reordered AMS payload would otherwise move the revision with
        # nothing semantically changed, and the dispatcher's final guard would
        # abort a perfectly good print with ``feed_state_changed``.
        "overlay": sorted(applied_overlay),
        "backup_membership": sorted([member, list(members)] for member, members in membership.items()),
    }
    if layout.direct_rack:
        # Every rack fact a plan here rests on — a swapped hotend, a moving rack, a
        # dock calibrated, filament at an inlet — is a new revision, so AutoQueue's
        # stale-plan check sees it before the upload, not at the final validate.
        payload["rack"] = rack.facts()
    revision = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return PrinterFeedSnapshot(
        printer_id,
        routing_model,
        bool(state and state.connected),
        getattr(state, "connection_generation", 0),
        revision,
        telemetry.ams_known,
        telemetry.ams_present,
        telemetry.external_known,
        tuple(sources),
        diameters,
        telemetry.fts,
        getattr(state, "ams_auto_switch_filament", None),
        incomplete,
        telemetry.left_tpu_firmware,
        getattr(state, "fts_pending_confirmation", False) is True,
        backup_membership=membership,
        rack=rack,
        layout=layout,
    )


def _rack_of(state, layout: TrayLayout) -> RackState:
    """A direct rack's hotends and holder from the printer's state (``utils/direct_rack``)."""
    if not layout.direct_rack or state is None:
        return NO_RACK
    from backend.app.utils.direct_rack import build_rack_state

    nozzle_rack = getattr(state, "nozzle_rack", None)
    return build_rack_state(
        nozzle_rack if isinstance(nozzle_rack, list) else None,
        layout,
        exist_mask=_int_or_none(getattr(state, "nozzle_exist_mask", None)),
        hc_mask=_int_or_none(getattr(state, "nozzle_hc_mask", None)),
        holder_status=_int_or_none(getattr(state, "rack_holder_status", None)),
        holder_info2=_int_or_none(getattr(state, "rack_holder_info2", None)),
        rack_type=_int_or_none(getattr(state, "direct_rack_type", None)),
        units=[uid for uid, _ in layout.units],
    )


def _int_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None
