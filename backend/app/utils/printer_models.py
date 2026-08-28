"""Printer model normalization utilities.

Converts 3MF printer model names (e.g., "Bambu Lab X1 Carbon") to
normalized short names (e.g., "X1C") that match database storage.
"""

# Map from 3MF printer_model strings to normalized short names
PRINTER_MODEL_MAP = {
    "Bambu Lab X1 Carbon": "X1C",
    "Bambu Lab X1": "X1",
    "Bambu Lab X1E": "X1E",
    "Bambu Lab P1S": "P1S",
    "Bambu Lab P1P": "P1P",
    "Bambu Lab P2S": "P2S",
    "Bambu Lab A1": "A1",
    "Bambu Lab A1 Mini": "A1 Mini",
    "Bambu Lab A1 mini": "A1 Mini",
    # Bambu cloud rolled out a terse model-code rename mid-2026 (#1649);
    # 3MFs prepared with newer cloud presets may carry this short form.
    "Bambu Lab A1M": "A1 Mini",
    "Bambu Lab H2D": "H2D",
    "Bambu Lab H2D Pro": "H2D Pro",
    "Bambu Lab H2C": "H2C",
    "Bambu Lab H2S": "H2S",
    "Bambu Lab X2D": "X2D",
    "Bambu Lab A2L": "A2L",
}

# Map from printer_model_id (internal codes in slice_info.config) to short names
# These are the codes Bambu Studio uses internally
PRINTER_MODEL_ID_MAP = {
    # X1 series (SSDP/MQTT codes — BS configs: X1C=BL-P001, X1=BL-P002, X1E=C13).
    # NB: C11/C12 are NOT X1 codes — they are P1P/P1S (see P1 series below).
    "BL-P001": "X1C",
    "BL-P002": "X1",
    "C13": "X1E",
    # P1 series (BS configs: P1P=C11, P1S=C12)
    "C11": "P1P",
    "C12": "P1S",
    "P1P": "P1P",
    "P1S": "P1S",
    # P2 series
    "P2S": "P2S",
    "N7": "P2S",  # SSDP/MQTT internal code for P2S
    # X2 series
    "N6": "X2D",
    # A2 series (A2L is single-FDM + integrated cutter/plotter — single nozzle)
    "N9": "A2L",
    # A1 series
    "A11": "A1",
    "A12": "A1 Mini",
    # N1 = A1 Mini, N2S = A1 — every other registry (firmware_check, VP manager
    # model + serial-prefix maps, printer_manager A1_MODELS) agrees; this map was
    # the lone outlier that had them flipped (surfaced scoping A2L, #1684).
    "N1": "A1 Mini",
    "N2S": "A1",
    "A04": "A1 Mini",
    # H2 series (Office/H series)
    "O1D": "H2D",
    "O1E": "H2D Pro",  # Some devices report O1E
    "O2D": "H2D Pro",  # Some devices report O2D
    "O1C": "H2C",
    "O1C2": "H2C",
    "O1S": "H2S",
}


# Rod/rail type classification for maintenance tasks.
# Carbon rods: X1, P1 series (CoreXY with carbon fiber rods)
# Steel rods: P2S, X2D series (hardened steel linear shafts)
# Linear rails: A1, H2 series (linear rail motion system)
# Values must be uppercase with spaces stripped for normalized comparison.
CARBON_ROD_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "X1",
        "X1C",
        "X1E",
        "P1P",
        "P1S",
        # Internal codes (hyphen-stripped for get_rod_type lookup).
        # X1 family: X1C=BLP001, X1=BLP002, X1E=C13. P1 family: P1P=C11, P1S=C12.
        "BLP001",  # X1C
        "BLP002",  # X1
        "C13",  # X1E
        "C11",  # P1P
        "C12",  # P1S
    ]
)

STEEL_ROD_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "P2S",
        "X2D",
        # Internal codes
        "N7",  # P2S
        "N6",  # X2D
    ]
)

LINEAR_RAIL_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "A1",
        "A1MINI",
        "A2L",
        "H2D",
        "H2DPRO",
        "H2C",
        "H2S",
        # Internal codes
        "N1",  # A1 Mini
        "N2S",  # A1
        "N9",  # A2L
        "A04",  # A1 Mini (alternate)
        "A11",  # A1
        "A12",  # A1 Mini
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
    ]
)


# Models without any external storage (MicroSD / SD card slot).
# The A1 and A1 Mini ship with internal storage only — there is no
# firmware-side "Store sent files on external storage" toggle and no
# slicer-side equivalent surfaces one. The connection diagnostic's
# external_storage check (printer_diagnostic.py) must skip on these
# models instead of reporting fail from a 0-valued home_flag bit (#1703).
NO_EXTERNAL_STORAGE_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "A1",
        "A1MINI",
        # Internal codes
        "N1",  # A1 Mini
        "N2S",  # A1
        "A04",  # A1 Mini (alternate)
        "A11",  # A1
        "A12",  # A1 Mini
    ]
)


# Models with an ethernet port.
# X1, P1P, A1, A1 Mini do NOT have ethernet.
ETHERNET_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "X1C",
        "X1E",
        "P1S",
        "P2S",
        "X2D",
        "H2D",
        "H2DPRO",
        "H2C",
        "H2S",
        # Internal codes (hyphen-stripped). X1C=BLP001, X1E=C13, P1S=C12 have
        # ethernet; plain X1 (BLP002) and P1P (C11) do NOT — their codes stay out.
        "BLP001",  # X1C
        "C13",  # X1E
        "C12",  # P1S
        "P1S",  # P1S (display)
        "N6",  # X2D
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
    ]
)


# Dual-nozzle (dual-extruder) printers. Single source of truth for nozzle
# class — consumed by ``BambuMQTTClient.start_print``, the K-profile routes,
# and the re-slice nozzle-class guard (previously an inline model tuple
# duplicated across all three). Re-slicing a model laid out for a single-nozzle
# printer onto one of these — or vice versa — is not yet supported: the source
# 3MF's embedded single-nozzle filament/extruder layout is not a valid
# dual-nozzle project and BambuStudio's multi-extruder validator rejects it.
DUAL_NOZZLE_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "H2D",
        "H2DPRO",
        "H2C",
        "X2D",
        # Internal codes
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "N6",  # X2D
    ]
)


# Printers with a swappable nozzle rack ("Vortek"): the H2C carries six hotends
# in a rack and mounts one of them at a time.
#
# ⚠️ **Why this needs its own set rather than reusing DUAL_NOZZLE_MODELS.** On
# every other dual-nozzle printer the dispatch ``nozzle_mapping`` values ARE the
# MQTT extruder indices (0 = right, 1 = left). On a rack model the wire wants
# the *physical* nozzle position for both carriages: the rack positions the
# firmware reports as IDs 16-21 (see ``device.nozzle.info`` in bambu_mqtt), and
# **1** for the fixed hotend — which is not its extruder index.
#
# ⚠️ The H2C does not follow the "0 = right" convention either: **extruder index
# 1 is the rack side**. Both values were settled by hardware A/B in upstream
# #2800 after the first attempt got both of them wrong — sending an extruder
# index where a physical position is expected makes the printer clean and level
# with one nozzle and then print with another, several millimetres off the bed.
NOZZLE_RACK_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "H2C",
        # Internal codes
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
    ]
)

# The extruder index the rack carriage answers to, and the physical nozzle id
# the fixed carriage answers to. Neither is derivable from the other models.
NOZZLE_RACK_EXTRUDER_INDEX = 1
FIXED_CARRIAGE_PHYSICAL_ID = 1


def is_nozzle_rack_model(model: str | None) -> bool:
    """Return True if the model mounts its nozzles from a swappable rack (H2C).

    Accepts both the display name and the internal SSDP code, because the model
    string carries whichever the printer row happens to hold.
    """
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in NOZZLE_RACK_MODELS


def has_ethernet(model: str | None) -> bool:
    """Return True if the printer model has an ethernet port."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in ETHERNET_MODELS


def has_external_storage(model: str | None) -> bool:
    """Return True if the printer model can have a MicroSD / external storage slot.

    Defaults to True when the model is unknown — the diagnostic only flips
    its check off for the explicit no-storage list. New models added to the
    Bambu lineup without a slot must be added to ``NO_EXTERNAL_STORAGE_MODELS``
    or the diagnostic will continue to evaluate ``store_to_sdcard`` against
    a hardware feature the printer doesn't have.
    """
    if not model:
        return True
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized not in NO_EXTERNAL_STORAGE_MODELS


def is_dual_nozzle_model(model: str | None) -> bool:
    """Return True if the printer model has two nozzles (H2D family / X2D)."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in DUAL_NOZZLE_MODELS


# ---------------------------------------------------------------------------
# Auto-calibration capability matrix (off / auto / on tri-state gating).
#
# Some models' firmware supports an *automatic* calibration mode (the printer
# itself decides whether the step is needed) in addition to plain off/on. The
# print dialog surfaces a 3-position control (off / auto / on) ONLY on models
# listed here; every other model keeps the 2-position off/on toggle. This is an
# axis INDEPENDENT of nozzle count — auto bed-leveling / flow-cali apply to
# single-nozzle models too (A2L, P2S, H2S).
#
# Source: BambuStudio resources/printers/*.json — `support_bed_leveling==2`
# (auto), `support_auto_flow_calibration`, `support_nozzle_offset_calibration`.
# Values must be uppercase with spaces/dashes stripped for normalized comparison.
AUTO_BED_LEVELING_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces/dashes)
        "A2L",
        "P2S",
        "H2S",
        "H2C",
        "H2D",
        "H2DPRO",
        "X2D",
        # Internal codes
        "N9",  # A2L
        "N7",  # P2S
        "O1S",  # H2S
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "N6",  # X2D
    ]
)

# Flow (extrusion) auto-calibration — same model set as auto bed-leveling
# (BambuStudio advertises both on the same machines). Aliased so the two can
# never drift apart by accident; split into its own frozenset only if a model
# ever supports one but not the other.
AUTO_FLOW_CALI_MODELS = AUTO_BED_LEVELING_MODELS

# Nozzle-offset auto-calibration — dual-nozzle models only (a nozzle *offset*
# only exists with two nozzles). Currently coincides with DUAL_NOZZLE_MODELS,
# but kept as its own matrix because the capability axes are independent.
AUTO_NOZZLE_OFFSET_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces/dashes)
        "H2C",
        "H2D",
        "H2DPRO",
        "X2D",
        # Internal codes
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "N6",  # X2D
    ]
)


def supports_auto_bed_leveling(model: str | None) -> bool:
    """Return True if the model's firmware supports *automatic* bed leveling
    (the 3-position off/auto/on control); False → 2-position off/on only."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in AUTO_BED_LEVELING_MODELS


def supports_auto_flow_cali(model: str | None) -> bool:
    """Return True if the model's firmware supports *automatic* flow (extrusion)
    calibration (the 3-position off/auto/on control)."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in AUTO_FLOW_CALI_MODELS


def supports_auto_nozzle_offset(model: str | None) -> bool:
    """Return True if the model's firmware supports *automatic* nozzle-offset
    calibration (the 3-position off/auto/on control). Dual-nozzle models only."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in AUTO_NOZZLE_OFFSET_MODELS


# NOTE: per-model DEVICE-calibration availability (lidar / bed-level / vibration
# / motor-noise / nozzle-offset / high-temp-bed / clump) is data-driven — read
# from the mirrored BambuStudio config JSONs by ``utils/printer_configs.py``, not
# a hardcoded table here. See that module + backend/app/data/printers/README.md.


# Models with a confirmed door-open sensor exposed via MQTT — split by WHICH
# field carries the door-open bit (bit 23 == 0x800000 in both cases):
#   * home_flag bit 23 — X1 family (X1 / X1C / X1E). Long-verified.
#   * stat bit 23      — X2D. Verified 2026-07-23 on real hardware: X2D's
#     home_flag bit 23 stays 0 regardless of the door, but ``stat`` bit 23 flips
#     cleanly (closed 0x40258000 -> open 0x40A58000). BambuStudio reads door
#     state from neither field, so this whole mechanism is BamDude-specific.
#   * stat bit 23 — P2S, INFERRED (not yet hardware-verified): Bambu ships ONE
#     door-sensor replacement part + guide for the P2S/X2D pair (identical
#     hardware), so P2S almost certainly reports on the same stat bit 23. If a
#     real P2S ever proves otherwise (stuck / flapping badge), drop N7/P2S here.
#
# Still absent: the H2 family (H2D/H2D Pro/H2C/H2S) HAS door sensors (hall-effect)
# but the MQTT field/bit is unknown — add only after capturing it on hardware.
# P1S has no door sensor at all; open-frame models (P1P, A1, A1 Mini) have no door.
#
# To add a model: confirm on real hardware which field's bit 23 flips on
# open/close, then add it to the matching set. Never add on protocol
# speculation. (Internal codes are hyphen-stripped for lookup:
# X1C=BLP001, X1=BLP002, X1E=C13, X2D=N6, P2S=N7.)
DOOR_SENSOR_HOME_FLAG_MODELS = frozenset(["X1", "X1C", "X1E", "BLP001", "BLP002", "C13"])
DOOR_SENSOR_STAT_MODELS = frozenset(["X2D", "N6", "P2S", "N7"])


def door_sensor_field(model: str | None) -> str | None:
    """Return which MQTT field carries this model's door-open bit (bit 23):
    ``"home_flag"`` for the X1 family, ``"stat"`` for X2D, or ``None`` when the
    model has no trustworthy door signal. See the model sets above.
    """
    if not model:
        return None
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    if normalized in DOOR_SENSOR_HOME_FLAG_MODELS:
        return "home_flag"
    if normalized in DOOR_SENSOR_STAT_MODELS:
        return "stat"
    return None


def has_door_sensor(model: str | None) -> bool:
    """Return True if the printer model exposes a trustworthy door-open signal
    over MQTT.

    Gates both the backend bit-23 parser and the frontend door-state badge —
    non-sensor models must not surface misleading "Door Closed" / "Door Open"
    state. See the door-sensor model sets above for the rationale.
    """
    return door_sensor_field(model) is not None


def get_rod_type(model: str | None) -> str | None:
    """Return the rod/rail type for a printer model.

    Returns:
        "carbon" for X1/P1 series (carbon fiber rods),
        "steel_rod" for P2S/X2D series (hardened steel rods),
        "linear_rail" for A1/H2 series (linear rails),
        None for unknown models.
    """
    if not model:
        return None
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    if normalized in CARBON_ROD_MODELS:
        return "carbon"
    if normalized in STEEL_ROD_MODELS:
        return "steel_rod"
    if normalized in LINEAR_RAIL_MODELS:
        return "linear_rail"
    return None


# G-code interchange families (#2578). A sliced 3MF may target a different model
# ONLY within its family: same kinematics, build volume and G-code dialect. The
# X1/P1 series is the one proven-interchangeable group (256mm CoreXY, single
# nozzle — mixed farms intentionally run X1-sliced jobs on P1S/P1P). Everything
# else is exact-match only; extend deliberately, never by assumption — a wrong
# entry here dispatches G-code onto hardware it was not sliced for. Short display
# names only (uppercase, no spaces); is_gcode_compatible() resolves internal
# codes (C11, O1D, ...) to short names before lookup.
GCODE_COMPAT_FAMILIES = (frozenset(["X1", "X1C", "X1E", "P1P", "P1S"]),)


def is_gcode_compatible(sliced_for_model: str | None, target_model: str | None) -> bool:
    """Return True when G-code sliced for one model may be dispatched to the other.

    Unknown/missing metadata on either side returns True — we can only validate
    what the 3MF declares, and legacy files without ``sliced_for_model`` must keep
    working.
    """
    if not sliced_for_model or not target_model:
        return True

    def _norm(model: str) -> str:
        # Internal codes (e.g. "C11") → short names first, so "C11" vs "X1C"
        # compares equal instead of leaning on family membership.
        resolved = PRINTER_MODEL_ID_MAP.get(model.strip(), model)
        return resolved.strip().upper().replace(" ", "").replace("-", "")

    a = _norm(sliced_for_model)
    b = _norm(target_model)
    if a == b:
        return True
    return any(a in family and b in family for family in GCODE_COMPAT_FAMILIES)


def normalize_printer_model_id(model_id: str | None) -> str | None:
    """Convert printer_model_id (internal code) to normalized short name.

    Args:
        model_id: The printer_model_id from slice_info.config (e.g., "C11", "O1D")

    Returns:
        Normalized short name (e.g., "X1C", "H2D") or the original ID if unknown.
    """
    if not model_id:
        return None

    # Check known mappings
    if model_id in PRINTER_MODEL_ID_MAP:
        return PRINTER_MODEL_ID_MAP[model_id]

    # Return original if unknown (might already be a short name)
    return model_id


def normalize_printer_model(raw_model: str | None) -> str | None:
    """Convert 3MF printer_model to normalized short name.

    Args:
        raw_model: The printer_model string from 3MF metadata
            (e.g., "Bambu Lab X1 Carbon")

    Returns:
        Normalized short name (e.g., "X1C") or None if input is empty.
        Unknown models have "Bambu Lab " prefix stripped.
    """
    if not raw_model:
        return None

    # Check known mappings first
    if raw_model in PRINTER_MODEL_MAP:
        return PRINTER_MODEL_MAP[raw_model]

    # Strip "Bambu Lab " prefix for unknown models
    stripped = raw_model.replace("Bambu Lab ", "").strip()
    return stripped or None


def normalize_model_name(raw: str | None) -> str | None:
    """Normalize any spelling of a printer model to its short name.

    ⚠️ **Internal codes are resolved FIRST, and the order is the whole point.**
    ``normalize_printer_model`` returns unknown input unchanged rather than
    None, so an ``normalize_printer_model(x) or normalize_printer_model_id(x)``
    chain never reaches the code map: "C12" is not in the name map, comes back
    as "C12" — truthy — and the second branch is dead. A queue item targeting
    that code then matches no printer row and waits for ever, saying only "No
    active C12 printers eligible". Running the code map first is a no-op for
    every input that is not a code (upstream `a9b57ccd`).

    Still returns the input unchanged when neither map knows it — a model we
    have never seen is not a reason to lose the operator's answer.
    """
    if not raw:
        return None
    return normalize_printer_model(normalize_printer_model_id(raw) or raw) or raw
