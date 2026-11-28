"""A printer's tray numbering, in one place (Bambu Studio's, protocol only).

A global tray id names one filament source of a printer: what ``tray_now``
reports, what a print's ``ams_mapping`` holds and what BamDude compares, books
and labels slots by. Bambu Studio derives it from the unit's AMS type, bits 0-3
of the unit's hex ``info`` (``DevAmsType``, ``DevFilaSystem::GetTrayIndexMap``)::

    AMS / AMS Lite / AMS 2 Pro (1, 2, 3)   ams_id * 4 + slot
    AMS-HT (4, N3S, units 128-135)         ams_id
    A2L AMS Lite (5, AMS_LITE_MIXED)       24 + slot
    AMS 3H (6, ZMULTI_A, unit 32)          28 + slot
    AMS 3P (7, ZMULTI_B, unit 33)          37 + slot
    external input                         its own vir_slot id

The A2L reports its AMS Lite as physical unit 16; BamDude stores it as unit 6
(normalised at the MQTT ingest), so ``6 * 4 + slot`` is already Studio's 24-27.
This module owns that fold (``normalize_am_unit_id``) and its way back
(``A2L_LITE_PHYSICAL_AMS_ID``).

A direct-connected nozzle rack (H3C: nine PrintTools, P3C: seven) is a single
toolhead that picks one hotend of the rack. Rack AMS slot ``k`` feeds PrintTool
``k`` and external input ``v`` feeds PrintTool ``255 - v``
(``DevAmsTray::get_nozzle_addr_id``); the PrintTool a person reads is that
address + 1, so external ``v`` is labelled ``Ext-{256 - v}``. Studio knows the
rack from ``print.device.holder.info2`` (bits 1-4, the rack type, above
``RackType_AB``); before a holder is seen — and the H3C's own frames carry
none — the mirrored model config's ``default_nozzle_count > 2`` says the same,
the very test Studio uses to label ``Ext-n``. The rack size is the number of
external inputs (``DevNozzleRack::GetTotalRackNozzleCount``), and until they
are seen the same config field.

A slot is stored as ``(ams_id, tray_id)``. Every external input is stored
under ams_id 255: ``254 -> 0``, ``255 -> 1`` (what every row already holds) and
``247..253 -> 2..8``. That index is not monotonic in the PrintTool, so labels,
ordering and bounds always go through the PrintTool (``external_print_tool``).

⚠️ Nothing outside this module (and its TypeScript twin
``frontend/src/utils/trayIds.ts``) computes, splits or labels a tray number by
hand — ``test_tray_numbers_have_one_home.py`` fails the build on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum


class AmsType(IntEnum):
    """Bambu Studio's ``DevAmsType``: bits 0-3 of an AMS unit's ``info``."""

    EXT_SPOOL = 0
    AMS = 1
    AMS_LITE = 2
    N3F = 3  # AMS 2 Pro
    N3S = 4  # AMS-HT
    AMS_LITE_MIXED = 5  # the A2L's AMS Lite
    ZMULTI_A = 6  # AMS 3H (H3C)
    ZMULTI_B = 7  # AMS 3P (P3C)


ZMULTI_TYPES = frozenset({AmsType.ZMULTI_A, AmsType.ZMULTI_B})
ZMULTI_TRAY_BASE: Mapping[AmsType, int] = {AmsType.ZMULTI_A: 28, AmsType.ZMULTI_B: 37}
#: Unit ids Studio gives the rack AMS types (``0x20`` / ``0x21``). 33 is read
#: from Studio's ``0x20 || 0x21`` branch and the ``2A21`` HMS codes, not seen.
ZMULTI_UNIT_IDS: Mapping[AmsType, int] = {AmsType.ZMULTI_A: 32, AmsType.ZMULTI_B: 33}
_ZMULTI_TYPE_BY_UNIT = {uid: kind for kind, uid in ZMULTI_UNIT_IDS.items()}
#: Bit of the rack AMS in ``ams_exist_bits`` (``DevFilaSystem``).
_ZMULTI_EXIST_BIT: Mapping[AmsType, int] = {AmsType.ZMULTI_A: 13, AmsType.ZMULTI_B: 14}
#: The largest rack in Studio's catalogue (the H3C); a bound for stored rows only.
MAX_RACK_SLOTS = 9

A2L_LITE_PHYSICAL_AMS_ID = 16
A2L_LITE_NORMALIZED_AMS_ID = 6
A2L_LITE_GLOBAL_BASE = 24

AMS_HT_FIRST_ID = 128
AMS_HT_IDS = range(128, 136)
REGULAR_UNIT_IDS = range(16)  # units whose slots are ams_id * 4 + slot
_WIRE_REGULAR_UNIT_IDS = range(4)  # units a print mapping / snow names by (ams << 8) | slot

EXTERNAL_AMS_ID = 255
EXTERNAL_DEPUTY_ID = 254  # VIRTUAL_TRAY_DEPUTY_ID
EXTERNAL_MAIN_ID = 255  # VIRTUAL_TRAY_MAIN_ID
DIRECT_RACK_EXTERNAL_IDS = range(247, 256)
NO_TRAY = 255  # tray_now: nothing loaded
WIRE_SLOT_NONE = 0xFFFF
_DIRECT_RACK_NOZZLE_COUNT = 2  # a direct rack has more than this many


def as_int(value: object) -> int | None:
    """An id field as an int (MQTT sends int or numeric str), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def normalize_am_unit_id(ams_id: int) -> int:
    """The A2L AMS Lite's physical unit id (16) as the one BamDude stores (6); other ids unchanged.

    Self-scoping: no other Bambu device reports an AMS unit at id 16.
    """
    return A2L_LITE_NORMALIZED_AMS_ID if ams_id == A2L_LITE_PHYSICAL_AMS_ID else ams_id


# --- unit type --------------------------------------------------------------


def ams_type_from_info(info: object) -> AmsType | None:
    """The AMS type in a unit's ``info`` (hex string or parsed int); None when absent, unreadable or 0."""
    if isinstance(info, bool):
        return None
    if isinstance(info, int):
        raw = info
    elif isinstance(info, str) and info.strip():
        try:
            raw = int(info.strip(), 16)
        except ValueError:
            return None
    else:
        return None
    try:
        kind = AmsType(raw & 0xF)
    except ValueError:
        return None
    return None if kind == AmsType.EXT_SPOOL else kind


def fallback_ams_type(ams_id: int) -> AmsType:
    """The type of a unit whose ``info`` was never seen, told from its id.

    A deviation from Studio, which takes such a unit for an AMS: for us that
    folds the AMS 3H (32) into the AMS-HT range and the A2L Lite (6) into AMS.
    """
    if ams_id == A2L_LITE_NORMALIZED_AMS_ID:
        return AmsType.AMS_LITE_MIXED
    if ams_id in _ZMULTI_TYPE_BY_UNIT:
        return _ZMULTI_TYPE_BY_UNIT[ams_id]
    if ams_id >= AMS_HT_FIRST_ID:  # Studio: 128-135; our readers always took 128+ as AMS-HT
        return AmsType.N3S
    return AmsType.AMS


def ams_type_of(ams_id: int, ams_type: object = None) -> AmsType:
    """The type of unit ``ams_id``: ``ams_type`` when it is a known unit type, else the id's.

    An id 128-135 is an AMS-HT whatever is passed.
    """
    if ams_id in AMS_HT_IDS:
        return AmsType.N3S
    if isinstance(ams_type, int) and not isinstance(ams_type, bool):
        try:
            kind = AmsType(ams_type)
        except ValueError:
            kind = None
        if kind is not None and kind != AmsType.EXT_SPOOL:
            return kind
    return fallback_ams_type(ams_id)


def unit_type(unit: object) -> tuple[int, AmsType] | None:
    """``(id, type)`` of one unit: a raw MQTT dict (``info``), a stamped / API dict (``ams_type``) or an object."""
    if isinstance(unit, Mapping):
        uid = as_int(unit.get("id"))
        explicit = unit.get("ams_type")
        info = unit.get("info")
    else:
        uid = as_int(getattr(unit, "id", None))
        explicit = getattr(unit, "ams_type", None)
        info = getattr(unit, "info", None)
    if uid is None:
        return None
    uid = normalize_am_unit_id(uid)
    from_info = ams_type_from_info(info)
    return uid, ams_type_of(uid, from_info if from_info is not None else explicit)


def unit_extruder(info: object) -> int | None:
    """The extruder a unit's ``info`` binds it to (bits 8-11, ``DevAms``), None when unreadable."""
    if isinstance(info, bool):
        return None
    if isinstance(info, int):
        raw = info
    else:
        try:
            raw = int(str(info), 16)
        except (TypeError, ValueError):
            return None
    return (raw >> 8) & 0xF


def stamp_ams_type(unit: dict) -> None:
    """Write the type the unit's ``info`` names onto it as ``ams_type``.

    Sticky: a delta without ``info`` keeps what an earlier frame said, because
    the unit merge keeps the key (Studio re-reads it on every parse; ours costs
    nothing to keep). A unit never seen with a type gets the id's.
    """
    if not isinstance(unit, dict):
        return
    uid = as_int(unit.get("id"))
    if uid is None:
        return
    from_info = ams_type_from_info(unit.get("info"))
    if from_info is not None:
        unit["ams_type"] = int(ams_type_of(uid, from_info))
    elif not isinstance(unit.get("ams_type"), int):
        unit["ams_type"] = int(fallback_ams_type(uid))


def is_zmulti(ams_type: object) -> bool:
    """Whether ``ams_type`` is a rack AMS (AMS 3H / AMS 3P): one slot per PrintTool."""
    return ams_type in ZMULTI_TYPES


def is_ams_ht_id(ams_id: object) -> bool:
    """Whether ``ams_id`` is an AMS-HT unit (128-135, one tray each)."""
    return isinstance(ams_id, int) and not isinstance(ams_id, bool) and ams_id in AMS_HT_IDS


# --- numbers ----------------------------------------------------------------


def is_external_id(tray_id: object) -> bool:
    """Whether a global id can only be an external input: 254 / 255 anywhere, 247-253 on a direct rack."""
    return isinstance(tray_id, int) and not isinstance(tray_id, bool) and tray_id in DIRECT_RACK_EXTERNAL_IDS


def external_tray_id(slot_index: int) -> int:
    """The global id of the external input stored as ``(255, slot_index)``: 0 -> 254, 1 -> 255, 2..8 -> 247..253."""
    if 2 <= slot_index <= 8:
        return DIRECT_RACK_EXTERNAL_IDS.start + slot_index - 2
    return EXTERNAL_DEPUTY_ID + slot_index


def external_slot_index(tray_id: int) -> int:
    """The ``tray_id`` external input ``tray_id`` is stored under: 254 -> 0, 255 -> 1, 247..253 -> 2..8."""
    if DIRECT_RACK_EXTERNAL_IDS.start <= tray_id < EXTERNAL_DEPUTY_ID:
        return tray_id - DIRECT_RACK_EXTERNAL_IDS.start + 2
    return tray_id - EXTERNAL_DEPUTY_ID


def external_print_tool(tray_id: int) -> int:
    """The PrintTool (1-based) a direct rack's external input ``tray_id`` feeds: ``256 - tray_id``."""
    return EXTERNAL_MAIN_ID + 1 - tray_id


def external_for_print_tool(print_tool: int) -> int:
    """The external input of PrintTool ``print_tool`` (1-based) on a direct rack."""
    return EXTERNAL_MAIN_ID + 1 - print_tool


def external_label(tray_id: int) -> str:
    """A direct rack's external input as Studio names it: ``Ext-{PrintTool}``."""
    return f"Ext-{external_print_tool(tray_id)}"


def global_tray_id(ams_id: int, slot: int, ams_type: object = None) -> int:
    """The global tray id of slot ``slot`` of unit ``ams_id`` (``(255, n)`` is an external input).

    ``(254, n)`` is read as ``(255, n)``, as every reader here did: 254 is never a unit.
    """
    if ams_id in (EXTERNAL_DEPUTY_ID, EXTERNAL_AMS_ID):
        return external_tray_id(slot)
    kind = ams_type_of(ams_id, ams_type)
    if kind == AmsType.N3S:
        return ams_id
    if kind == AmsType.AMS_LITE_MIXED:
        return A2L_LITE_GLOBAL_BASE + slot
    if kind in ZMULTI_TYPES:
        return ZMULTI_TRAY_BASE[kind] + slot
    return ams_id * 4 + slot


def legacy_tray_index(ams_id: int, slot: int) -> int:
    """The firmware's flat ``ams_id * 4 + slot``, which is not a global id outside a regular AMS.

    What ``M620 R<n>``, ``ams_filament_setting`` / ``extrusion_cali_*``'s
    ``tray_id`` on units 0-3 and the A2L's physical slot carry.
    """
    return ams_id * 4 + slot


def regular_slot_key(tray_id: int) -> tuple[int, int]:
    """``(unit, slot)`` of a regular AMS's global id ``tray_id`` (``// 4``, ``% 4``)."""
    return tray_id // 4, tray_id % 4


def local_slot(tray_id: int) -> int:
    """The slot within its unit a regular AMS / AMS-HT global id names (an AMS-HT's one tray is 0)."""
    if tray_id >= AMS_HT_FIRST_ID:
        return 0
    return tray_id % 4


def tray_flag_bit(ams_id: int, slot: int, ams_type: object = None) -> int | None:
    """Bit of a slot in the ``tray_*_bits`` masks (``DevAmsTray::get_tray_flag_bit``), else None.

    An AMS-HT at ``16 + (ams_id - 128)``, the A2L Lite and a rack AMS at their
    global id, a regular AMS (units 0-15) at ``ams_id * 4 + slot``.
    """
    kind = ams_type_of(ams_id, ams_type)
    if kind == AmsType.N3S:
        return 16 + (ams_id - AMS_HT_FIRST_ID) if is_ams_ht_id(ams_id) else None
    if kind == AmsType.AMS_LITE_MIXED or kind in ZMULTI_TYPES:
        return global_tray_id(ams_id, slot, kind)
    if ams_id in REGULAR_UNIT_IDS:
        return legacy_tray_index(ams_id, slot)
    return None


def ams_exist_bit(ams_id: int, ams_type: object = None) -> int | None:
    """Bit of a unit in ``ams_exist_bits``: an AMS its id, AMS-HT ``4 + (id - 128)``, A2L Lite 12, rack AMS 13 / 14."""
    kind = ams_type_of(ams_id, ams_type)
    if kind == AmsType.N3S:
        return 4 + (ams_id - AMS_HT_FIRST_ID) if is_ams_ht_id(ams_id) else None
    if kind == AmsType.AMS_LITE_MIXED:
        return 12
    if kind in ZMULTI_TYPES:
        return _ZMULTI_EXIST_BIT[kind]
    return ams_id if 0 <= ams_id < 16 else None


def bit_is_set(mask: int, bit: int) -> bool:
    """Whether bit ``bit`` of a presence mask (up to bit 45 — Python ints, never 32-bit) is set."""
    return bool((mask >> bit) & 1)


def parse_mask(value: object) -> int | None:
    """A firmware bitmask (hex string, or int) as an int; None when absent or unreadable."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        mask = int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return mask if mask >= 0 else None


def filam_bak_members(mask: int, ams_type_by_unit: Mapping[int, AmsType] | None = None) -> list[int]:
    """Global tray ids of a ``filam_bak`` backup-group mask.

    Bits 0-15 the regular AMS slots, 16-23 the AMS-HT units 128-135, 24-27 the
    A2L Lite. Bits 28-36 are the AMS 3H's ``28 + slot`` when the printer has
    one. A ZMULTI_B group is not shown: Studio reads both rack types' bits as
    ``28 + slot`` (its ``37`` branch is unreachable), so on a P3C such a group
    names no tray in Studio either.
    """
    members = [bit for bit in range(16) if bit_is_set(mask, bit)]
    members.extend(AMS_HT_FIRST_ID + bit for bit in range(8) if bit_is_set(mask, 16 + bit))
    members.extend(A2L_LITE_GLOBAL_BASE + bit for bit in range(4) if bit_is_set(mask, 24 + bit))
    kinds = set((ams_type_by_unit or {}).values())
    if AmsType.ZMULTI_A in kinds:
        base = ZMULTI_TRAY_BASE[AmsType.ZMULTI_A]
        members.extend(base + bit for bit in range(MAX_RACK_SLOTS) if bit_is_set(mask, base + bit))
    return members


# --- the wire form (ams_id << 8) | slot -------------------------------------


def wire_parts(value: object, *, unit_byte: bool = False) -> tuple[int, int] | None:
    """``(value >> 8, value & 0xFF)`` of any non-negative int, with no "none" check; None otherwise.

    For the readers that judge the parts themselves: a ``snow`` of 0xFFFF is
    ``(255, 255)`` "unloaded", and the firmware's initial 0xFF00FF a unit part
    no branch matches. ``unit_byte`` keeps only the low byte of the unit part.
    """
    encoded = as_int(value)
    if encoded is None or encoded < 0:
        return None
    unit = encoded >> 8
    return (unit & 0xFF if unit_byte else unit), encoded & 0xFF


def split_wire_slot(value: object) -> tuple[int, int] | None:
    """``(ams_id, slot)`` of a value the firmware encodes as ``(ams_id << 8) | slot``; None when not one.

    ``print.mapping``, ``ams.csl``, FilaSwitch ``in`` and the extruder / nozzle
    ``snow`` use it; 0xFFFF and above are "none".
    """
    encoded = as_int(value)
    if encoded is None or not 0 <= encoded < WIRE_SLOT_NONE:
        return None
    return wire_parts(encoded, unit_byte=True)


def wire_local_slot(slot: int) -> int:
    """The slot within a regular AMS a wire slot carries (its two low bits, as every reader took it)."""
    return slot & 0x03


def encode_wire_slot(ams_id: int, slot: int) -> int:
    """``(ams_id << 8) | slot``."""
    return (ams_id << 8) | slot


# --- labels -----------------------------------------------------------------

ZMULTI_NAMES: Mapping[AmsType, str] = {AmsType.ZMULTI_A: "AMS-3H", AmsType.ZMULTI_B: "AMS-3P"}


def unit_letter(ams_id: int) -> str:
    """The letter of a unit: A.. for AMS 0-15 and for AMS-HT 128-135; the id itself otherwise."""
    if is_ams_ht_id(ams_id):
        return chr(65 + ams_id - AMS_HT_FIRST_ID)
    if 0 <= ams_id < 26:
        return chr(65 + ams_id)
    return str(ams_id)


def unit_name(ams_id: int, ams_type: object = None) -> str:
    """A unit's short name: ``AMS-A``, ``HT-A``, ``AMS-3H``, ``AMS-3P``."""
    kind = ams_type_of(ams_id, ams_type)
    if kind in ZMULTI_TYPES:
        return ZMULTI_NAMES[kind]
    if kind == AmsType.N3S:
        return f"HT-{unit_letter(ams_id)}"
    return f"AMS-{unit_letter(ams_id)}"


def usage_slot_label(ams_id: int, tray_id: int) -> str:
    """A stored slot as the print-finished message names it: ``AMS-A T1``, ``HT-A``, ``AMS-3H T6``, ``Ext``.

    An AMS-HT is its unit (it used to read "Ext", which it is not); a direct
    rack's external input (index 2..8, or any on a rack) is ``Ext-n``.
    """
    if ams_id in (EXTERNAL_DEPUTY_ID, EXTERNAL_AMS_ID):
        return external_label(external_tray_id(tray_id)) if 2 <= tray_id <= 8 else "Ext"
    kind = ams_type_of(ams_id)
    if kind == AmsType.N3S:
        return unit_name(ams_id, kind)
    return f"{unit_name(ams_id, kind)} T{tray_id + 1}"


# --- one printer's layout ---------------------------------------------------


def direct_rack_type_from_holder(holder: object) -> int | None:
    """The rack type ``print.device.holder.info2`` names (bits 1-4), else None.

    Studio: ``holder.info2`` present -> ``RackType`` from bits 1-4; above
    ``RackType_AB`` (0) is a direct-connected rack. ``holder.info`` alone is the
    H2C's (RackType_AB) — 0 here, not direct.
    """
    if not isinstance(holder, Mapping):
        return None
    if "info2" in holder:
        info2 = as_int(holder.get("info2"))
        if info2 is None:
            return None
        return (info2 >> 1) & 0xF
    if "info" in holder:
        return 0
    return None


def state_is_direct_rack(state: object) -> bool:
    """Whether a printer state is a direct-connected rack's (H3C, P3C), told from the state alone.

    The holder says so when it was seen; the H3C's own frames carry none, so a
    rack AMS (its stamped type) or a nozzle entry with a dock (``addr_id``) is
    the evidence the state holds.
    """
    rack_type = getattr(state, "direct_rack_type", None)
    if isinstance(rack_type, int) and not isinstance(rack_type, bool):
        return rack_type > 0
    raw = getattr(state, "raw_data", None) or {}
    units = raw.get("ams") if isinstance(raw, Mapping) else None
    if isinstance(units, Mapping):
        units = units.get("ams")
    for unit in units if isinstance(units, list) else []:
        pair = unit_type(unit)
        if pair is not None and is_zmulti(pair[1]):
            return True
    nozzles = getattr(state, "nozzle_rack", None)
    return any(isinstance(n, Mapping) and n.get("addr_id") is not None for n in nozzles or [])


def names_loaded_source(tray: object, state: object) -> bool:
    """Whether a ``tray_now`` value names a loaded filament source: 0-254 anywhere.

    255 is "nothing loaded" — except on a direct rack whose toolhead ``snow`` said
    PrintTool 1's input (255) is the tray now (``PrinterState.direct_rack_input_loaded``,
    written only by that parse).
    """
    if not isinstance(tray, int) or isinstance(tray, bool):
        return False
    if 0 <= tray <= EXTERNAL_DEPUTY_ID:
        return True
    return tray == NO_TRAY and getattr(state, "direct_rack_input_loaded", False) is True


def model_is_direct_rack(model: str | None) -> bool:
    """The mirrored config's word on a direct rack: ``default_nozzle_count > 2`` (Studio's ``Ext-n`` test)."""
    return model_nozzle_count(model) > _DIRECT_RACK_NOZZLE_COUNT


def model_nozzle_count(model: str | None) -> int:
    """``default_nozzle_count`` of the model's mirrored config; 0 when unknown."""
    from backend.app.utils.printer_configs import default_nozzle_count

    return default_nozzle_count(model)


@dataclass(frozen=True)
class TrayLayout:
    """How one printer numbers its trays.

    ``units``: its AMS units as ``(ams_id, type)``. ``direct_rack``: a single
    toolhead with a direct-connected rack of ``rack_size`` PrintTools, one
    external input each (never "two nozzles"). ``main_input_loaded``: on a
    direct rack, PrintTool 1's input (255) is the tray now — the toolhead's
    ``snow`` 0xFF00, which a bare ``tray_now`` 255 cannot tell from "nothing".
    """

    units: tuple[tuple[int, AmsType], ...] = ()
    direct_rack: bool = False
    rack_size: int = 0
    main_input_loaded: bool = False

    @classmethod
    def build(
        cls,
        units: Iterable[object] | None,
        vt_trays: Iterable[object] | None = None,
        *,
        rack_type: int | None = None,
        model: str | None = None,
        main_input_loaded: bool = False,
    ) -> TrayLayout:
        """Layout of a unit list and an external-input list, a holder's rack type and the model.

        Direct rack: the holder's rack type when one was seen, else the model's
        config. Rack size: the number of external inputs when more than two are
        known, else the config's nozzle count. ``main_input_loaded`` counts only
        on a direct rack.
        """
        pairs: list[tuple[int, AmsType]] = []
        seen: set[int] = set()
        for unit in units or ():
            pair = unit_type(unit)
            if pair is not None and pair[0] not in seen:
                seen.add(pair[0])
                pairs.append(pair)
        direct = rack_type > 0 if rack_type is not None else model_is_direct_rack(model)
        size = 0
        if direct:
            externals = [v for v in vt_trays or () if v is not None]
            size = len(externals) if len(externals) > _DIRECT_RACK_NOZZLE_COUNT else model_nozzle_count(model)
        return cls(
            units=tuple(pairs), direct_rack=direct, rack_size=size, main_input_loaded=direct and main_input_loaded
        )

    @classmethod
    def from_state(cls, state: object, model: str | None = None) -> TrayLayout:
        """Layout of a printer's live MQTT state (``raw_data`` ams / vt_tray, ``direct_rack_type``)."""
        raw = getattr(state, "raw_data", None) if state is not None else None
        units: object = None
        vt_trays: object = None
        if isinstance(raw, Mapping):
            units = raw.get("ams")
            if isinstance(units, Mapping):
                units = units.get("ams")
            vt_trays = raw.get("vt_tray")
            if isinstance(vt_trays, Mapping):
                vt_trays = [vt_trays]
        rack_type = getattr(state, "direct_rack_type", None) if state is not None else None
        input_loaded = getattr(state, "direct_rack_input_loaded", False) if state is not None else False
        return cls.build(
            units if isinstance(units, list) else None,
            vt_trays if isinstance(vt_trays, list) else None,
            rack_type=rack_type if isinstance(rack_type, int) else None,
            model=model,
            main_input_loaded=input_loaded is True,
        )

    def ams_type(self, ams_id: int) -> AmsType:
        """Type of unit ``ams_id``; told from the id when the unit isn't listed."""
        for uid, kind in self.units:
            if uid == ams_id:
                return kind
        return fallback_ams_type(ams_id)

    @property
    def rack_unit(self) -> tuple[int, AmsType] | None:
        """The rack AMS as ``(ams_id, type)``, when one is listed."""
        for uid, kind in self.units:
            if kind in ZMULTI_TYPES:
                return uid, kind
        return None

    def slot_limit(self, ams_id: int) -> int:
        """How many slots unit ``ams_id`` may have: a rack AMS the rack size, an AMS-HT 1, else 4."""
        kind = self.ams_type(ams_id)
        if kind in ZMULTI_TYPES:
            return self.rack_size if self.direct_rack and self.rack_size else MAX_RACK_SLOTS
        if kind == AmsType.N3S:
            return 1
        return 4

    def global_tray_id(self, ams_id: int, slot: int) -> int:
        """Global tray id of slot ``slot`` of unit ``ams_id`` (``(255, n)`` an external input)."""
        if ams_id == EXTERNAL_AMS_ID:
            return external_tray_id(slot)
        return global_tray_id(ams_id, slot, self.ams_type(ams_id))

    def is_external(self, tray_id: object) -> bool:
        """Whether global tray ``tray_id`` is one of this printer's external inputs."""
        if not isinstance(tray_id, int) or isinstance(tray_id, bool):
            return False
        if tray_id in (EXTERNAL_DEPUTY_ID, EXTERNAL_MAIN_ID):
            return True
        return self.direct_rack and tray_id in DIRECT_RACK_EXTERNAL_IDS

    def external_in_rack(self, tray_id: int) -> bool:
        """Whether a direct rack has external input ``tray_id``: its PrintTool is 1..rack size."""
        return self.direct_rack and 1 <= external_print_tool(tray_id) <= self.rack_size

    def external_label(self, tray_id: int) -> str | None:
        """``Ext-n`` of a direct rack's external input; None off a direct rack (callers keep their own)."""
        return external_label(tray_id) if self.direct_rack and tray_id in DIRECT_RACK_EXTERNAL_IDS else None

    def rack_label(self, tray_id: int) -> str | None:
        """A direct rack's source as people read it — ``AMS-3H 5``, ``Ext-3`` — else None (callers keep theirs)."""
        if not self.direct_rack:
            return None
        rack = self.rack_slot(tray_id)
        if rack is not None:
            return f"{unit_name(rack[0], self.ams_type(rack[0]))} {rack[1] + 1}"
        return self.external_label(tray_id)

    def rack_slot(self, tray_id: int) -> tuple[int, int] | None:
        """``(ams_id, slot)`` of a rack AMS's global id, else None.

        28-36 / 37-45 are a rack AMS's (units 32 / 33), as in Studio, unless the
        printer lists a regular unit 7 or above whose slots would sit there —
        no Bambu printer has one, so a stored rack slot keeps its key without
        live data (upstream's rule).
        """
        candidates: list[tuple[int, AmsType]] = []
        listed = self.rack_unit
        if listed is not None:
            candidates.append(listed)
        elif self.direct_rack or not any(
            uid in REGULAR_UNIT_IDS and uid >= 7 and kind not in ZMULTI_TYPES for uid, kind in self.units
        ):
            candidates = [(uid, kind) for kind, uid in ZMULTI_UNIT_IDS.items()]
        for uid, kind in candidates:
            base = ZMULTI_TRAY_BASE[kind]
            if base <= tray_id < base + self.slot_limit(uid):
                return uid, tray_id - base
        return None

    def slot_key(self, tray_id: int) -> tuple[int, int]:
        """The ``(ams_id, tray_id)`` global tray ``tray_id`` is stored under."""
        if tray_id in DIRECT_RACK_EXTERNAL_IDS:
            return EXTERNAL_AMS_ID, external_slot_index(tray_id)
        if tray_id >= AMS_HT_FIRST_ID:
            return tray_id, 0
        rack = self.rack_slot(tray_id)
        if rack is not None:
            return rack
        return regular_slot_key(tray_id)

    def raw_slot_to_global(self, raw: int) -> int | None:
        """A direct rack's bare ``tray_now`` / ``tray_tar`` / ``tray_pre`` slot ``raw`` as a global id.

        Only ``0 <= raw < rack size`` and only when the rack AMS is listed (its
        type picks 28 or 37); anything else is None.
        """
        listed = self.rack_unit
        if not self.direct_rack or listed is None or not 0 <= raw < self.rack_size:
            return None
        return global_tray_id(listed[0], raw, listed[1])

    def is_journal_tray(self, tray_id: int) -> bool:
        """Whether a ``tray_now`` value names a physical filament source rather than "nothing loaded".

        255 is "nothing loaded", and every colour change passes through it — out
        on every printer, except on a direct rack whose toolhead ``snow`` says
        PrintTool 1's input (255) is loaded (``main_input_loaded``).
        """
        if tray_id == NO_TRAY:
            return self.direct_rack and self.main_input_loaded
        if self.direct_rack:
            if self.rack_slot(tray_id) is not None:
                return True
            if tray_id in DIRECT_RACK_EXTERNAL_IDS:
                return self.external_in_rack(tray_id)
        return (
            0 <= tray_id <= 15
            or A2L_LITE_GLOBAL_BASE <= tray_id < A2L_LITE_GLOBAL_BASE + 4
            or is_ams_ht_id(tray_id)
            or tray_id == EXTERNAL_DEPUTY_ID
        )

    def accepts_slot(self, ams_id: int, tray_id: int) -> bool:
        """Whether ``(ams_id, tray_id)`` can name a slot of this printer (assignment requests)."""
        if tray_id < 0:
            return False
        if ams_id == EXTERNAL_AMS_ID:
            if tray_id in (0, 1):
                return True
            return self.direct_rack and 2 <= tray_id <= 8 and self.external_in_rack(external_tray_id(tray_id))
        kind = self.ams_type(ams_id)
        if kind in ZMULTI_TYPES:
            listed = any(uid == ams_id for uid, _ in self.units)
            return (self.direct_rack or listed) and tray_id < self.slot_limit(ams_id)
        return tray_id <= 3

    def print_tool(self, tray_id: int) -> int | None:
        """The PrintTool (1-based) a direct rack's source feeds: rack slot k -> k + 1, external v -> 256 - v."""
        if not self.direct_rack:
            return None
        rack = self.rack_slot(tray_id)
        if rack is not None:
            return rack[1] + 1
        if tray_id in DIRECT_RACK_EXTERNAL_IDS:
            return external_print_tool(tray_id)
        return None

    def decode_wire_slot(self, value: object) -> int | None:
        """Global tray id of a ``(ams_id << 8) | slot`` value (mapping, ``snow``), None when it names none.

        Units 0-3 (slot bits ``& 3``) and the AMS-HT as always; a rack AMS by
        its type; the external input: on a direct rack its own id, elsewhere
        254, or 255 for slot 255 (as every reader here did).
        """
        split = split_wire_slot(value)
        if split is None:
            return None
        ams_id, slot = split
        if self.direct_rack and ams_id in DIRECT_RACK_EXTERNAL_IDS:
            return ams_id
        if ams_id in (EXTERNAL_DEPUTY_ID, EXTERNAL_MAIN_ID):
            return EXTERNAL_MAIN_ID if slot == 255 else EXTERNAL_DEPUTY_ID
        if is_ams_ht_id(ams_id):
            return ams_id
        if ams_id in _WIRE_REGULAR_UNIT_IDS:
            return legacy_tray_index(ams_id, wire_local_slot(slot))
        kind = self.ams_type(ams_id)
        if kind in ZMULTI_TYPES and slot < self.slot_limit(ams_id):
            return ZMULTI_TRAY_BASE[kind] + slot
        return None


NO_LAYOUT = TrayLayout()


def as_layout(value: object) -> TrayLayout:
    """``value`` when it is a ``TrayLayout``, else no layout — what a stand-in client or manager answers."""
    return value if isinstance(value, TrayLayout) else NO_LAYOUT


def decode_wire_slot(value: object, layout: TrayLayout | None = None) -> int | None:
    """``TrayLayout.decode_wire_slot`` with no layout known (a non-rack printer's reading)."""
    return (layout or NO_LAYOUT).decode_wire_slot(value)


def slot_key(tray_id: int, layout: TrayLayout | None = None) -> tuple[int, int]:
    """``TrayLayout.slot_key`` with no layout known."""
    return (layout or NO_LAYOUT).slot_key(tray_id)
