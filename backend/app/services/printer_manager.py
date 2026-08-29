import asyncio
import concurrent.futures
import logging
import re
import time
import traceback
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.tasks import spawn_background_task
from backend.app.models.printer import Printer
from backend.app.schemas.printer import AirductFan
from backend.app.services.bambu_mqtt import (
    FAN_CTRL,
    BambuMQTTClient,
    PrinterState,
    airduct_fan_control,
    airduct_mode_effective,
    airduct_parts_effective,
    get_stage_name,
)
from backend.app.utils.printer_configs import airduct_fan_label, get_device_support_flags
from backend.app.utils.printer_storage import storage_capability_for
from backend.app.utils.temperature_limits import limits_for
from backend.app.utils.timelapse import capability_for as timelapse_capability_for

logger = logging.getLogger(__name__)

# How long a macro wait tolerates a dropped connection before giving up. These
# links flap: one farm's support log shows 11 disconnects and 20 reconnects in
# 9h24m, four of them ``rc=Unspecified error``. Long enough to ride that out,
# short enough that a genuinely dead printer is not waited on for minutes.
MACRO_DISCONNECT_GRACE_SECONDS = 30.0


async def _sync_kprofiles_for_printer(printer_id: int) -> None:
    """Async coroutine wired into ``on_kprofiles_changed``: opens a fresh
    DB session and pulls the printer's live K-profile list into the
    ``filament_calibration`` cache. Wraps :func:`sync_printer_kprofiles_to_cache`
    so callers don't need to construct a session themselves."""
    from backend.app.core.database import async_session
    from backend.app.models.filament_calibration import FilamentCalibration
    from backend.app.services.calibration_service import sync_printer_kprofiles_to_cache
    from backend.app.services.kprofile_autolink import propagate_calibration_to_spools

    try:
        async with async_session() as db:
            await sync_printer_kprofiles_to_cache(db=db, printer_id=printer_id)
            # Re-link spools whose resolved filament_id matches any of this
            # printer's calibrations (fresh K-profiles auto-attach to spools).
            fids = set(
                (
                    await db.execute(
                        select(FilamentCalibration.filament_id).where(FilamentCalibration.printer_id == printer_id)
                    )
                )
                .scalars()
                .all()
            )
            await propagate_calibration_to_spools(db=db, printer_id=printer_id, filament_ids=fids)
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("Auto-sync/link of K-profiles for printer %s failed: %s", printer_id, e)


# Models that have a real chamber temperature sensor
# Based on Home Assistant Bambu Lab integration
# P1P/P1S and A1/A1Mini do NOT have chamber temp sensors
# Includes both display names and internal codes from MQTT/SSDP
CHAMBER_TEMP_SUPPORTED_MODELS = frozenset(
    [
        # Display names
        "X1",
        "X1C",
        "X1E",  # X1 series
        "X2D",  # X2 series
        "P2S",  # P2 series
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",  # H2 series
        # Internal codes (from MQTT/SSDP)
        "BL-P001",  # X1C
        "BL-P002",  # X1 — was missing while its sibling was here, so a printer
        # identified by the raw code rather than the display name lost its
        # chamber reading from the card, the status API and the history, silently.
        "C13",  # X1E
        "N6",  # X2D
        "O1D",  # H2D
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate code)
        "N7",  # P2S
    ]
)

# Models that may incorrectly report stg_cur=0 when idle (firmware bug)
# Based on Home Assistant Bambu Lab integration observations
# See: https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/models.py
A1_MODELS = frozenset(
    [
        # Display names
        "A1",
        "A1 MINI",
        "A1-MINI",
        "A1MINI",
        # Internal codes (from MQTT/SSDP)
        "N1",  # A1 Mini
        "N2S",  # A1
    ]
)

# Models affected by the stg_cur=0 idle bug (firmware reports stg_cur=0 when idle,
# which maps to "Printing" in STAGE_NAMES and overrides the correct IDLE state)
STG_CUR_IDLE_BUG_MODELS = A1_MODELS | frozenset(
    [
        # Display names
        "P1P",
        "P1S",
        # Internal codes (from MQTT/SSDP)
        "C11",  # P1P
        "C12",  # P1S
    ]
)


def _norm_model(model: str | None) -> str:
    """Normalise a model name for set membership.

    ⚠️ **Internal spaces too**, not just the ends. The three chamber
    sets used ``model.strip().upper()``, which leaves the space in the middle of
    ``"H2D Pro"`` — and ``"H2D Pro"`` is exactly what ``PRINTER_MODEL_ID_MAP``
    emits, while the set spells it ``H2DPRO``. So the H2D Pro answered False to
    every chamber question and preheat never heated its chamber; ``X1 Carbon``
    failed the same way. Matches ``ams_capabilities._norm`` and
    ``printer_configs._norm``, which already got this right.

    ⚠️ Spaces only — **hyphens are load-bearing here.** The internal codes in
    these sets are spelled ``BL-P001`` / ``BL-P002``, and the A1 set lists
    ``A1-MINI`` explicitly. Stripping hyphens the way ``ams_capabilities._norm``
    does would silently unmatch every X1-family internal code; those sets
    tolerate it because they are written without hyphens, and these are not.
    """
    if not model:
        return ""
    return model.strip().upper().replace(" ", "")


def supports_chamber_temp(model: str | None) -> bool:
    """Check if a printer model has a real chamber temperature sensor.

    P1P, P1S, A1, and A1Mini do NOT have chamber temp sensors.
    The 'chamber_temper' value they report is meaningless.
    """
    return _norm_model(model) in CHAMBER_TEMP_SUPPORTED_MODELS


# Models with an ACTIVE chamber heater (chamber temp raisable via set_ctt, not just
# readable). Deliberately a subset of CHAMBER_TEMP_SUPPORTED_MODELS: X1/X1C/P2S report
# chamber temp but heat the chamber only passively via bed radiation, so they are
# sensor-capable but NOT heater-capable. X1E has a heater but no airduct flap; P2S has
# an airduct flap but no heater — the three chamber sets are intentionally distinct.
# Used by the preheat / heat-soak stage (#1468) to decide whether to send set_ctt.
CHAMBER_HEATER_MODELS = frozenset(
    [
        # Display names
        "X1E",
        "X2D",
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",
        # Internal codes (from MQTT/SSDP)
        "C13",  # X1E
        "N6",  # X2D
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate code)
        "O1S",  # H2S
    ]
)


def supports_chamber_heater(model: str | None) -> bool:
    """Check if a printer model has an ACTIVE chamber heater (set_ctt-controllable),
    not merely a chamber temperature sensor.

    Distinct from ``supports_chamber_temp``: X1/X1C/P2S read chamber temp but warm the
    chamber only passively via bed radiation, so they are sensor-capable but not
    heater-capable. The preheat / heat-soak stage (#1468) sends set_ctt only on models in
    this set; sensor-only models wait for radiant warm-up, no-sensor models soak on a timer.

    **Answered from the mirrored config.** ``support_chamber_temp_edit`` is
    exactly this question, and its value across the fifteen shipped files
    reproduces the hardcoded set exactly — X1E, X2D, H2C, H2D, H2D Pro, H2S —
    so the set was a transcription with nothing of its own to say. The list is
    kept only as the fallback for a model we ship no config for.
    """
    cfg = get_device_support_flags(model)
    if "support_chamber_temp_edit" in cfg:
        return bool(cfg["support_chamber_temp_edit"])
    return _norm_model(model) in CHAMBER_HEATER_MODELS


# Models with a cooling / heating airduct flap. Same set as the PrintersPage airduct
# toggle (P2S, X2D, H2D, H2C, H2S, H2D Pro). X1E has a chamber heater but NO airduct flap
# (fixed front-door inlet); P2S has an airduct flap but no active heater. The preheat
# stage cares about the heater∩airduct intersection: when set_ctt fires it must also assert
# heating mode, otherwise the default cooling flap actively vents and fights the heater.
CHAMBER_AIRDUCT_MODELS = frozenset(
    [
        # Display names
        "P2S",
        "X2D",
        "H2C",
        "H2D",
        "H2DPRO",
        "H2S",
        # Internal codes (from MQTT/SSDP)
        "N7",  # P2S
        "N6",  # X2D
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate code)
        "O1S",  # H2S
    ]
)


def supports_airduct(model: str | None) -> bool:
    """Check if a printer model has a cooling / heating airduct mode toggle.

    Distinct from ``supports_chamber_heater`` — P2S has the airduct toggle but no active
    heater, and X1E has the heater but no airduct. The preheat stage flips the flap to
    heating before energising the chamber (cooling mode vents it and fights the heater).

    ⚠️ Stays a model list on purpose: **the mirrored configs do not answer this
    one.** Only N6 (X2D) and N7 (P2S) carry a ``fan`` block; the H2 family has
    none at all, yet those machines do have the duct. See
    ``inv-per-model-capability-from-mirrored-config`` — a hardcoded set is
    legitimate where the data is silent, provided it says so.
    """
    return _norm_model(model) in CHAMBER_AIRDUCT_MODELS


def has_stg_cur_idle_bug(model: str | None) -> bool:
    """Check if a printer model may incorrectly report stg_cur=0 when idle.

    Some firmware versions report stg_cur=0 (which maps to "Printing")
    even when the printer is idle. Originally observed on A1/A1 Mini via the
    Home Assistant Bambu Lab integration, also confirmed on P1S.
    """
    if not model:
        return False
    model_upper = model.strip().upper()
    return model_upper in STG_CUR_IDLE_BUG_MODELS


# BS ``MachineObject::is_in_printing_status`` (DeviceManager.cpp) — the four
# gcode_state values that mean "this machine has a job on it". Note SLICING and
# PREPARE: a print being prepared is already heating and positioning, which is
# exactly when homing or jogging does the most damage. An earlier, narrower
# version of this rule lived in ``firmware_batch._is_printing`` as
# ``("RUNNING", "PAUSE")`` and let PREPARE through.
BUSY_PRINT_STATES = frozenset({"RUNNING", "PAUSE", "SLICING", "PREPARE"})


def is_printer_busy(printer_id: int) -> bool:
    """Whether the printer has a job on it, so physical commands must be refused.

    BS can answer this question in the UI — it is a single-window desktop app,
    and a greyed-out button is a sufficient guard. Ours is an HTTP surface
    reachable by API key, by the Telegram bot and by a browser tab left open
    since before the print started, so the answer has to live on the server.

    A printer we have no client for is **not** reported busy: "unknown" is
    already handled by the connection check every caller does first, and
    answering True here would turn a disconnect into a permanent refusal.
    """
    client = printer_manager.get_client(printer_id)
    if not client or not client.state:
        return False
    return client.state.state in BUSY_PRINT_STATES


# ``is_bed_slinger`` moved to ``utils.printer_configs``, beside the
# ``printer_arch`` value it reads — ``bambu_mqtt`` needs the same flip for the
# axis jog and cannot import this module back. Import it from there; a second
# copy of a rule that decides which way a nozzle moves is not an option, and
# neither is a re-export that hides where it lives.


# Minimum firmware versions for AMS drying support (confirmed via capture testing)
# Keys are exact model names (upper-cased). Do NOT use substring matching - it would
# incorrectly gate X1E (matched by "X1") and H2D Pro (matched by "H2D").
_DRYING_MIN_FIRMWARE: dict[str, str] = {
    "H2D": "01.02.30.00",
    "H2S": "01.02.00.00",
    "X1": "01.09.00.00",
    "X1C": "01.09.00.00",
    # P1P/P1S deliberately absent (upstream #2533): the 01.08 floor was when P1
    # firmware gained AMS 2 Pro *support*, not remote drying, and it was never
    # verified against a live P1. See _DRYING_SCREEN_ONLY_MODELS below.
    "P2S": "01.02.00.00",
    "N7": "01.02.00.00",  # P2S internal model code
    "H2C": "01.02.00.00",  # AMS drying enabled at this floor (#1624)
    "O1C": "01.02.00.00",  # H2C SSDP model code (single nozzle)
    "O1C2": "01.02.00.00",  # H2C SSDP model code (dual nozzle)
}
# Models that definitely don't support AMS drying (no AMS 2 Pro / AMS-HT compatibility)
_DRYING_UNSUPPORTED_MODELS = frozenset({"A1", "A1MINI", "A1-MINI", "A1 MINI", "O1S", "N1", "N2S"})

# Models whose AMS *can* dry, but ONLY from the printer's own touchscreen (upstream
# #2533). Bambu's P1 manual is explicit: "P1S connected AMS drying functions may only
# be controlled from the P1S screen." The firmware still answers
# ``ams_filament_drying`` with result: success and then discards it, so nothing we
# publish can start (or stop) a cycle on any P1 firmware — which is why P1P/P1S are
# no longer firmware-gated in _DRYING_MIN_FIRMWARE.
#
# Distinct from _DRYING_UNSUPPORTED_MODELS: an A1 has no drying-capable AMS at all,
# a P1S has one it just can't be told to use. The UI needs to tell those apart, so
# it keeps the control visible-but-disabled here rather than dropping it.
#
# Internal SSDP codes sit alongside the display names because ``Printer.model`` holds
# whatever the printer or user gave us — same reason _DRYING_MIN_FIRMWARE carries "N7"
# and _DRY_WHILE_PRINTING_MIN_FIRMWARE carries "O1E"/"N6"/"BL-P001". C11=P1P, C12=P1S
# (see utils/printer_models.PRINTER_MODEL_ID_MAP). Without them a P1S row stored as
# "C12" falls through to the allow-by-default branch below with no gate at all.
_DRYING_SCREEN_ONLY_MODELS = frozenset({"P1P", "P1S", "C11", "C12"})


def drying_screen_only(model: str | None) -> bool:
    """True when the model's AMS dries only via the printer's own screen (#2533).

    Implies ``supports_drying() is False``. Published separately so the UI can say
    *where* to dry instead of silently hiding the control.
    """
    if not model:
        return False
    return model.strip().upper() in _DRYING_SCREEN_ONLY_MODELS


# Temperature keys the UI actually draws. ⚠️ ``state.temperatures`` is also
# WORKING MEMORY: it carries private bookkeeping (``_nozzle_target_set_time``)
# and derived flags (``nozzle_heating``) that no consumer outside this module
# should see. The full-status path hands out the whole dict to logged-in
# callers; the streaming overlay gets only this list, because an overlay token
# is a narrower grant than a login and must not pick up fields by accident as
# the dict grows.
DISPLAY_TEMPERATURE_KEYS = (
    "nozzle",
    "nozzle_target",
    "nozzle_2",
    "nozzle_2_target",
    "bed",
    "bed_target",
    "chamber",
    "chamber_target",
)


def display_temperatures(temperatures: dict | None, model: str | None) -> dict[str, float]:
    """Filter ``state.temperatures`` down to the readings a viewer is shown.

    ⚠️ Drops chamber readings on models without a real chamber sensor — P1P,
    P1S, A1 and A1 mini all report a meaningless ``chamber_temper`` — matching
    what ``printer_state_to_dict`` already does for the full status payload. The
    overlay must never put a measurement on screen that does not exist.
    """
    if not temperatures:
        return {}
    allow_chamber = supports_chamber_temp(model)
    out: dict[str, float] = {}
    for key in DISPLAY_TEMPERATURE_KEYS:
        if key.startswith("chamber") and not allow_chamber:
            continue
        value = temperatures.get(key)
        if value is None:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def uniform_tray_drying_hint(loaded_trays: list[tuple[str, object]]) -> tuple[str | None, int | None]:
    """Guess a running cycle's filament and target temperature from the trays.

    Bambu never echoes back which filament or temperature a drying cycle is
    running, so the badge normally reads the target cached when we sent the
    command. This is the fallback for when there is no record — drying started
    in a previous backend lifetime, or from the printer's own screen.

    ⚠️ It answers only when every loaded tray holds the SAME filament type. On a
    mixed unit the first tray is evidence of nothing: an AMS holding two PETG
    and two PLA spools, drying PLA at the 45 °C the user picked, was labelled
    "PETG @ 65°C" purely because slot 1 happened to hold PETG. Saying nothing
    and letting the badge show the countdown alone beats naming a temperature
    the cycle is not using.

    ⚠️ The temperature comes from the first slot that carries one, not from slot
    1 — a third-party spool in slot 1 has no RFID value, and giving up there
    threw away the answer the identical Bambu spool in slot 2 was holding.

    Args:
        loaded_trays: ``(tray_type, drying_temp)`` per tray, in slot order.
            Empty slots (falsy ``tray_type``) are ignored; ``drying_temp`` is
            the RFID-recommended value and may be missing or unparseable.

    Returns:
        ``(filament, temp)``, either of which may be None.
    """
    types = {str(tray_type) for tray_type, _ in loaded_trays if tray_type}
    if len(types) != 1:
        return None, None
    filament = next(iter(types))
    for tray_type, drying_temp in loaded_trays:
        if not tray_type or not drying_temp:
            continue
        try:
            return filament, int(drying_temp)
        except (TypeError, ValueError):
            continue
    return filament, None


def supports_drying(model: str | None, firmware: str | None) -> bool:
    """Check if a printer model supports AMS drying commands.

    Known models with confirmed min firmware get version-gated.
    Known unsupported models are blocked, as are screen-only models (#2533).
    All other models (H2D Pro, X1E, future models) are allowed -
    the command fails gracefully with result: "fail" if unsupported.
    """
    if not model:
        return False
    model_upper = model.strip().upper()
    if model_upper in _DRYING_UNSUPPORTED_MODELS:
        return False
    if model_upper in _DRYING_SCREEN_ONLY_MODELS:
        return False
    if model_upper in _DRYING_MIN_FIRMWARE:
        return bool(firmware and firmware >= _DRYING_MIN_FIRMWARE[model_upper])
    # For all other models: allow
    return True


# Minimum firmware versions for AMS "Print While Drying" — drying that runs CONCURRENTLY
# with an active print. Strictly stricter than _DRYING_MIN_FIRMWARE (idle drying). Verified
# against Bambu wiki release notes — the canonical phrasing on every supported model is
# "printing while filament is drying" / "Print While Drying". Models absent from the wiki
# release notes (A1, A1 Mini, P1*, X1 non-C, X1E) are intentionally excluded — the firmware
# will reject the command in those cases anyway via dry_sf_reason=[0] (TaskOccupied).
_DRY_WHILE_PRINTING_MIN_FIRMWARE: dict[str, str] = {
    "H2D": "01.03.00.00",
    "H2D PRO": "01.02.00.00",
    "H2DPRO": "01.02.00.00",
    "O1E": "01.02.00.00",  # H2D Pro SSDP code
    "O2D": "01.02.00.00",  # H2D Pro alternate code
    "H2C": "01.02.00.00",
    "O1C": "01.02.00.00",  # H2C SSDP code
    "O1C2": "01.02.00.00",  # H2C dual-nozzle SSDP code
    "H2S": "01.02.00.00",
    "X2D": "01.01.00.00",
    "N6": "01.01.00.00",  # X2D internal code
    "X1C": "01.11.02.00",
    "BL-P001": "01.11.02.00",  # X1C internal code
    "P2S": "01.02.00.00",
    "N7": "01.02.00.00",  # P2S internal code
    "A2L": "01.01.00.00",
    "N9": "01.01.00.00",  # A2L internal code
}


def supports_drying_while_printing(model: str | None, firmware: str | None) -> bool:
    """Check if a printer model+firmware supports running AMS drying CONCURRENTLY
    with an active print.

    Distinct from supports_drying() — that gates idle drying. This gate is strict:
    only models explicitly confirmed by Bambu wiki release notes are allowed.
    On unsupported models the firmware returns dry_sf_reason=[0] (TaskOccupied)
    while a print is running, so being conservative here costs nothing — the
    firmware is the ultimate arbiter, this gate just hides UI affordances.
    """
    if not model:
        return False
    model_upper = model.strip().upper()
    if model_upper not in _DRY_WHILE_PRINTING_MIN_FIRMWARE:
        return False
    return bool(firmware and firmware >= _DRY_WHILE_PRINTING_MIN_FIRMWARE[model_upper])


# AMS ``dry_sf_reason`` codes → human-readable blockers. Sourced from firmware
# observations in upstream #971. When one of these codes is present in an AMS
# push_status the firmware silently drops the drying command, so we surface
# them explicitly on the API route instead of returning a fake success.
DRYING_BLOCKING_REASONS: dict[int, str] = {
    0: "Printer is busy",
    1: "Insufficient power — too many AMS drying or external PSU required",
    2: "AMS is busy",
    3: "Filament is at the AMS outlet — retract it first",
    4: "AMS is already starting a drying cycle",
    5: "Not supported in 2D mode",
    6: "AMS is already drying",
    7: "AMS firmware is upgrading",
    8: "Plug in the external AMS power adapter to start drying",
}


def first_drying_blocking_reason(ams_unit: dict | None) -> tuple[int, str] | None:
    """Return the first blocking reason in an AMS unit's ``dry_sf_reason`` list.

    Returns ``(code, message)`` when at least one known blocker code is present,
    or ``None`` when the AMS is free to start drying. Unknown / malformed codes
    are skipped (fail-open) so a future firmware addition doesn't break existing
    clients that haven't been updated — they'll just see a regular drying-start
    error instead of a human-readable one.
    """
    if not ams_unit:
        return None
    for raw in ams_unit.get("dry_sf_reason") or []:
        try:
            code = int(raw)
        except (TypeError, ValueError):
            continue
        message = DRYING_BLOCKING_REASONS.get(code)
        if message:
            return code, message
    return None


def find_ams_unit(raw_data: dict | None, ams_id: int) -> dict | None:
    """Locate an AMS unit dict inside a printer push_status payload by id."""
    if not raw_data:
        return None
    for unit in raw_data.get("ams") or []:
        try:
            if int(unit.get("id", -1)) == ams_id:
                return unit
        except (TypeError, ValueError):
            continue
    return None


async def _record_skipped_as_defective(printer_id: int, skipped: list) -> None:
    """Async coroutine wired into ``on_skipped_objects_changed``: raise the
    running print's defective-part counter to the number of skipped objects.

    A skipped object is a part the operator gave up on, which is what the
    counter means. The value is set to ``max(current, len(skipped))`` rather
    than incremented, for two reasons: the callback carries the whole list (so a
    re-send of the same list must not add anything), and an operator who typed a
    higher number by hand — parts that printed but came out unusable — must not
    have it pulled back down by a later skip.

    Targets the printer's running archive the same way the skip-objects endpoint
    does (newest row at ``status='printing'``). No archive means nothing to
    record: skips only happen mid-print, so this is a lost race, not a state to
    repair.
    """
    if not skipped:
        return

    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive

    try:
        async with async_session() as db:
            archive = (
                (
                    await db.execute(
                        select(PrintArchive)
                        .where(PrintArchive.printer_id == printer_id)
                        .where(PrintArchive.status == "printing")
                        .order_by(PrintArchive.id.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if archive is None:
                logger.debug("No running archive for printer %s — skipped objects not recorded", printer_id)
                return

            from backend.app.models.archive_part import PrintArchivePart

            rows = (
                (await db.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id)))
                .scalars()
                .all()
            )
            id_rows = [r for r in rows if r.identify_ids]
            if id_rows:
                # Per-part: intersect the (total, not delta) skipped list with
                # each row's instance ids. max() keeps a hand-raised number.
                skipped_set = set(skipped)
                for row in id_rows:
                    hit = len(skipped_set & set(row.identify_ids))
                    if hit > (row.defective or 0):
                        row.defective = hit
                total = sum(r.defective or 0 for r in rows)
                new_count = max(archive.defective_count or 0, total)
            else:
                # Legacy archives without part rows: count-only, as before.
                new_count = max(archive.defective_count or 0, len(skipped))

            if new_count == archive.defective_count and not id_rows:
                return

            archive.defective_count = new_count
            await db.commit()
            logger.info(
                "Archive %s: defective parts raised to %d from %d skipped object(s) on printer %s",
                archive.id,
                new_count,
                len(skipped),
                printer_id,
            )
    except Exception as e:  # noqa: BLE001 — a counter must never break the MQTT path
        logger.warning("Failed to record skipped objects as defective for printer %s: %s", printer_id, e)


class PrinterInfo:
    """Basic printer info for callbacks."""

    def __init__(self, name: str, serial_number: str):
        self.name = name
        self.serial_number = serial_number


class PrinterManager:
    """Manager for multiple printer connections."""

    def __init__(self):
        self._clients: dict[int, BambuMQTTClient] = {}
        self._models: dict[int, str | None] = {}  # Cache printer models for feature detection
        self._connected_at: dict[int, float] = {}  # Unix timestamp of last connection
        self._printer_info: dict[int, PrinterInfo] = {}  # Cache printer name/serial for callbacks
        self._on_print_start: Callable[[int, dict], None] | None = None
        self._on_print_complete: Callable[[int, dict], None] | None = None
        self._on_print_running_observed: Callable[[int, dict], None] | None = None
        self._on_finish_photo_moment: Callable[[int, dict], None] | None = None
        self._on_status_change: Callable[[int, PrinterState], None] | None = None
        self._on_ams_change: Callable[[int, list], None] | None = None
        self._on_layer_change: Callable[[int, int, int], None] | None = None
        # #1349: fires when an AMS on the connected printer finishes a
        # drying cycle. Receives ``(printer_id, ams_id)``.
        self._on_drying_complete: Callable[[int, int], None] | None = None
        self._on_assignment_verified: Callable[[int, int, int, bool, dict], None] | None = None
        self._on_tray_change: Callable[[int, int, int], None] | None = None
        # Usage-journal events beyond tray changes (runout / spool_loaded).
        # Receives ``(printer_id, event, kind, global_tray_id, layer_num)``.
        self._on_usage_event: Callable[[int, str, str | None, int | None, int], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Track who started the current print (Issue #206)
        self._current_print_user: dict[int, dict] = {}  # {printer_id: {"user_id": int, "username": str}}
        # Plate-clear gate for queue flow, persisted to Printer.awaiting_plate_clear (m010, #961).
        # Semantically INVERTED vs the old _plate_cleared set: presence means the printer is
        # WAITING for user confirmation (blocked from auto-dispatch). Absence means the gate is
        # clear and the scheduler may proceed. Rehydrated from DB at startup so an Auto Off
        # power cycle can't silently bypass a pending confirmation.
        self._awaiting_plate_clear: set[int] = set()
        # Macro completion waiters: dispatch pipeline registers an Event here,
        # _broadcast_macro_complete sets it when stg_cur transitions to idle.
        self._macro_waiters: dict[int, tuple[asyncio.Event, dict]] = {}

    def get_printer(self, printer_id: int) -> PrinterInfo | None:
        """Get printer info by ID."""
        return self._printer_info.get(printer_id)

    def set_current_print_user(self, printer_id: int, user_id: int, username: str):
        """Track who started the current print (Issue #206)."""
        self._current_print_user[printer_id] = {"user_id": user_id, "username": username}

    def get_current_print_user(self, printer_id: int) -> dict | None:
        """Get the user who started the current print (Issue #206)."""
        return self._current_print_user.get(printer_id)

    def clear_current_print_user(self, printer_id: int):
        """Clear the current print user when print completes (Issue #206)."""
        self._current_print_user.pop(printer_id, None)

    def is_awaiting_plate_clear(self, printer_id: int) -> bool:
        """Returns True when the printer's queue is blocked on user plate-clear confirmation."""
        return printer_id in self._awaiting_plate_clear

    def set_awaiting_plate_clear(self, printer_id: int, awaiting: bool) -> None:
        """Arm or release the plate-clear gate. Persists to ``Printer.awaiting_plate_clear``
        asynchronously so an Auto Off power cycle can't drop the flag (#961).

        Also broadcasts an updated ``printer_status`` over the WebSocket
        (#1128). ``awaiting_plate_clear`` is a BamDude-side flag — toggling
        it does not produce an MQTT push from the printer, so without an
        explicit broadcast any UI subscriber that's NOT the originating tab
        would stay stale until the next coincidental status refresh. Any
        path that flips the flag (admin script, second tab, automation
        hitting ``POST /printers/{id}/clear-plate`` directly) is covered
        without each call site having to remember to broadcast.
        """
        if awaiting:
            self._awaiting_plate_clear.add(printer_id)
        else:
            self._awaiting_plate_clear.discard(printer_id)
        # Guard at the higher layer so we don't create coroutines that
        # ``_schedule_async`` would silently drop (otherwise Python emits
        # "coroutine was never awaited" warnings — visible in sync unit
        # tests that instantiate ``PrinterManager()`` without attaching a
        # loop).
        if self._loop and self._loop.is_running():
            self._schedule_async(self._persist_awaiting_plate_clear(printer_id, awaiting))
            self._schedule_async(self._broadcast_status_change(printer_id))

    async def _broadcast_status_change(self, printer_id: int) -> None:
        """Emit a ``printer_status`` WebSocket update for this printer (#1128).

        Used for state changes that don't come from MQTT — currently just
        the ``awaiting_plate_clear`` flag, but any future BamDude-side flag
        added to ``printer_state_to_dict`` should plumb through here too.
        The existing MQTT-driven broadcast in ``main.on_printer_status_change``
        deduplicates on a status_key that intentionally excludes BamDude
        flags, so flags need their own emit. Lazy-imports ``ws_manager`` to
        keep this module clean of application-layer infra at import time.
        """
        state = self.get_status(printer_id)
        if not state:
            # Printer disconnected — nothing to broadcast. The next reconnect
            # produces a fresh status push anyway.
            return
        try:
            from backend.app.core.websocket import ws_manager

            await ws_manager.send_printer_status(
                printer_id,
                printer_state_to_dict(
                    state,
                    printer_id,
                    self.get_model(printer_id),
                    self.get_drying_targets(printer_id),
                ),
            )
        except Exception as e:
            logger.warning(
                "Failed to broadcast printer_status after BamDude-side state change for printer %d: %s",
                printer_id,
                e,
            )

    async def _persist_awaiting_plate_clear(self, printer_id: int, awaiting: bool) -> None:
        """Best-effort DB write for the awaiting-plate-clear flag. Swallows errors
        (connection issues shouldn't break the in-memory scheduler gate)."""
        try:
            from backend.app.core.database import async_session

            async with async_session() as db:
                result = await db.execute(select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                if printer and printer.awaiting_plate_clear != awaiting:
                    printer.awaiting_plate_clear = awaiting
                    await db.commit()
        except Exception as e:  # pragma: no cover — persistence is best-effort
            logger.warning("Failed to persist awaiting_plate_clear for printer %s: %s", printer_id, e)

    async def load_awaiting_plate_clear_from_db(self) -> None:
        """Rehydrate the in-memory set from Printer.awaiting_plate_clear at startup (#961)."""
        from backend.app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(select(Printer.id).where(Printer.awaiting_plate_clear.is_(True)))
            ids = [row[0] for row in result.all()]
        self._awaiting_plate_clear = set(ids)
        if ids:
            logger.info("Restored awaiting_plate_clear gate for %d printer(s): %s", len(ids), ids)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for async callbacks."""
        self._loop = loop

    def set_print_start_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for print start events."""
        self._on_print_start = callback

    def set_print_complete_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for print completion events."""
        self._on_print_complete = callback

    def set_print_running_observed_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for restart-recovery RUNNING-state observations (#1485
        follow-up). Fires the first time we see ``state == RUNNING`` for a
        printer that started its print before BamDude came up — the #1304
        guard suppresses ``on_print_start`` for these, so anything that
        normally hangs off it (e.g. timelapse baseline capture) needs this
        hook to recover."""
        self._on_print_running_observed = callback

    def set_finish_photo_moment_callback(self, callback: Callable[[int, dict], None]):
        """Set callback for the #1721 finish-photo moment.

        Fires on the stage-22 ("Filament unloading") edge at end-of-print
        — the framing window where the toolhead is parked but the bed
        hasn't dropped yet. Falls back to firing at the FINISH-state
        transition for prints that skip stage 22 (cancel, external-spool-
        only, HMS halt, firmware variants). Payload includes the
        ``trigger`` key (``"stage_22"`` or ``"finish_state"``) and
        ``timelapse_was_active`` so the photo path can choose between
        live-camera capture and timelapse last-frame extraction."""
        self._on_finish_photo_moment = callback

    def set_status_change_callback(self, callback: Callable[[int, PrinterState], None]):
        """Set callback for status change events."""
        self._on_status_change = callback

    def set_ams_change_callback(self, callback: Callable[[int, list], None]):
        """Set callback for AMS data change events."""
        self._on_ams_change = callback

    def set_layer_change_callback(self, callback: Callable[[int, int, int], None]):
        """Set callback for layer change events.

        Receives ``(printer_id, layer_num, previous_layer)``.
        """
        self._on_layer_change = callback

    def set_drying_complete_callback(self, callback: Callable[[int, int], None]):
        """Set callback for AMS drying completion events (#1349).

        Receives ``(printer_id, ams_id)``. Fires once per falling edge of
        ``dry_time`` (>0 → 0) for each AMS unit on the connected printer.
        """
        self._on_drying_complete = callback

    def set_assignment_verified_callback(self, callback: Callable[[int, int, int, bool, dict], None]):
        """Set callback for spool-assignment read-back verification (upstream #2582).

        Receives ``(printer_id, ams_id, tray_id, verified, detail)``. Fires once
        per assignment either when the tray telemetry confirms the pushed
        filament id or when the verification window elapses without it.
        """
        self._on_assignment_verified = callback

    def set_tray_change_callback(self, callback: Callable[[int, int, int], None]):
        """Set callback for mid-print tray changes.

        Receives ``(printer_id, global_tray_id, layer_num)`` for every entry
        appended to the printer's tray-change log, so it can be persisted for
        the completion-time weight split.
        """
        self._on_tray_change = callback

    def set_usage_event_callback(self, callback: Callable[[int, str, str | None, int | None, int], None]):
        """Set callback for usage-journal events beyond tray changes.

        Receives ``(printer_id, event, kind, global_tray_id, layer_num)`` —
        ``event`` is ``"runout"`` (kind pause/autoswitch/external/ambiguous)
        or ``"spool_loaded"`` (replacement detected after a runout). main.py
        mirrors these into the ``print_usage_events`` journal.
        """
        self._on_usage_event = callback

    def _schedule_async(self, coro):
        """Schedule an async coroutine from a sync context.

        Captures exceptions from the coroutine and logs them to prevent
        silent failures in callbacks.
        """
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)

            def handle_exception(f):
                try:
                    # This will re-raise any exception from the coroutine
                    f.result()
                except (concurrent.futures.CancelledError, asyncio.CancelledError):
                    # Loop is shutting down — coroutines get cancelled by design.
                    # Silently swallow so we don't pollute the shutdown log.
                    pass
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).error(f"Exception in scheduled callback: {e}", exc_info=True)

            future.add_done_callback(handle_exception)

    async def connect_printer(self, printer: Printer) -> bool:
        """Connect to a printer."""
        # Capture the outgoing client (if any) before tearing it down — its
        # print-lifecycle flags are carried onto the replacement so a print
        # that finished during a stale window is still recognised as
        # complete. See BambuMQTTClient.carry_print_lifecycle_from.
        prior_client = self._clients.get(printer.id)
        if printer.id in self._clients:
            self.disconnect_printer(printer.id)

        printer_id = printer.id

        def on_state_change(state: PrinterState):
            if self._on_status_change:
                self._schedule_async(self._on_status_change(printer_id, state))

        def on_print_start(data: dict):
            if self._on_print_start:
                self._schedule_async(self._on_print_start(printer_id, data))

        def on_print_complete(data: dict):
            if self._on_print_complete:
                self._schedule_async(self._on_print_complete(printer_id, data))

        def on_print_running_observed(data: dict):
            if self._on_print_running_observed:
                self._schedule_async(self._on_print_running_observed(printer_id, data))

        def on_finish_photo_moment(data: dict):
            if self._on_finish_photo_moment:
                self._schedule_async(self._on_finish_photo_moment(printer_id, data))

        def on_ams_change(ams_data: list):
            if self._on_ams_change:
                self._schedule_async(self._on_ams_change(printer_id, ams_data))

        def on_layer_change(layer_num: int, previous_layer: int):
            if self._on_layer_change:
                self._schedule_async(self._on_layer_change(printer_id, layer_num, previous_layer))

        def on_drying_complete(ams_id: int):
            if self._on_drying_complete:
                self._schedule_async(self._on_drying_complete(printer_id, ams_id))

        def on_assignment_verified(ams_id: int, tray_id: int, verified: bool, detail: dict):
            if self._on_assignment_verified:
                self._schedule_async(self._on_assignment_verified(printer_id, ams_id, tray_id, verified, detail))

        def on_tray_change(tray_global: int, layer_num: int):
            if self._on_tray_change:
                self._schedule_async(self._on_tray_change(printer_id, tray_global, layer_num))

        def on_usage_event(event: str, kind: str | None, tray_global: int | None, layer_num: int):
            if self._on_usage_event:
                self._schedule_async(self._on_usage_event(printer_id, event, kind, tray_global, layer_num))

        def on_macro_complete(macro_name: str, status: str):
            self._schedule_async(self._broadcast_macro_complete(printer_id, macro_name, status))

        def on_kprofiles_changed():
            # Hash-diff in the MQTT client already filtered out duplicate
            # broadcasts, so this fires exactly when the printer's profile
            # list actually changed (connect / set / edit / delete / save).
            self._schedule_async(_sync_kprofiles_for_printer(printer_id))

        def on_skipped_objects_changed(skipped: list):
            self._schedule_async(_record_skipped_as_defective(printer_id, skipped))

        def on_first_status(live_state: str, live_file: str, live_subtask_id: str = "", live_subtask_name: str = ""):
            # First full status after each fresh connect — run the reconcile
            # sweep so a print that finished while BamDude was stopped or
            # disconnected (or a ghost-replay under a new subtask) gets its
            # archive closed and queue advanced.
            from backend.app.services.print_reconciliation import reconcile_printer_prints

            self._schedule_async(
                reconcile_printer_prints(printer_id, live_state, live_file, live_subtask_id, live_subtask_name)
            )

        client = BambuMQTTClient(
            ip_address=printer.ip_address,
            serial_number=printer.serial_number,
            access_code=printer.access_code,
            model=printer.model,
            on_state_change=on_state_change,
            on_print_start=on_print_start,
            on_print_complete=on_print_complete,
            on_ams_change=on_ams_change,
            on_layer_change=on_layer_change,
            on_macro_complete=on_macro_complete,
            on_kprofiles_changed=on_kprofiles_changed,
            on_first_status=on_first_status,
            on_drying_complete=on_drying_complete,
            on_print_running_observed=on_print_running_observed,
            on_finish_photo_moment=on_finish_photo_moment,
            on_assignment_verified=on_assignment_verified,
            on_skipped_objects_changed=on_skipped_objects_changed,
            on_tray_change=on_tray_change,
            on_usage_event=on_usage_event,
        )

        # Carry print-tracking state across the client recreation so a
        # mid-print stale reconnect doesn't lose completion detection.
        if prior_client is not None:
            client.carry_print_lifecycle_from(prior_client)

        client.connect()
        self._clients[printer_id] = client
        self._connected_at[printer_id] = time.time()
        self._models[printer_id] = printer.model  # Cache model for feature detection
        self._printer_info[printer_id] = PrinterInfo(printer.name, printer.serial_number)

        # Active poll until paho's `_on_connect` flips `state.connected`, capped
        # at 10s. Replaces the prior fixed `await asyncio.sleep(1)` which raced
        # against parallel reconnects during bulk dispatch (4+ printers running
        # `ensure_fresh_connection_for_printer` simultaneously made some TLS
        # handshakes miss the 1-second budget and the dispatcher would error
        # out with "Can`t re-connect printer MQTT" even though the connection
        # arrived a moment later). Healthy network: returns within ~100-300 ms
        # like before. Fully offline: still returns False, just after 10s
        # instead of 1s — the slower failure path is the trade-off for not
        # spuriously failing on a busy moment.
        for _ in range(100):  # 100 × 100 ms = 10 s
            if client.state.connected:
                break
            await asyncio.sleep(0.1)

        # Trigger a one-shot 3MF download retry for any fallback archives
        # on this printer — now that we're back online, the file may be
        # reachable.
        if client.state.connected:
            try:
                from backend.app.services.archive_download_retry import archive_download_retry

                spawn_background_task(
                    archive_download_retry.retry_printer_archives(printer_id),
                    name=f"retry-printer-archives-{printer_id}",
                )
            except Exception as e:
                logger.debug("Failed to schedule 3MF retry on printer %s connect: %s", printer_id, e)

        return client.state.connected

    def disconnect_printer(self, printer_id: int, timeout: float = 0):
        """Disconnect from a printer."""
        if printer_id in self._clients:
            self._clients[printer_id].disconnect(timeout=timeout)
            del self._clients[printer_id]
        self._connected_at.pop(printer_id, None)  # Clean up connection timestamp
        self._models.pop(printer_id, None)  # Clean up model cache
        self._printer_info.pop(printer_id, None)  # Clean up printer info cache

    def disconnect_all(self, timeout: float = 0):
        """Disconnect from all printers."""
        for printer_id in list(self._clients.keys()):
            self.disconnect_printer(printer_id, timeout=timeout)

    def get_status(self, printer_id: int) -> PrinterState | None:
        """Get the current status of a printer (checks for stale connections)."""
        if printer_id in self._clients:
            client = self._clients[printer_id]
            # Check staleness and update connected state if needed
            client.check_staleness()
            return client.state
        return None

    # Gcode states in which a job is loaded / in progress and cutting power
    # would ruin the print. PAUSE is included on purpose — a paused print is
    # still loaded on the bed. Used by the smart-plug auto-off guard (#1890) so
    # a re-print started from the touchscreen isn't killed mid-print.
    ACTIVE_PRINT_STATES = ("RUNNING", "PAUSE", "PREPARE", "SLICING")

    def is_print_active(self, printer_id: int) -> bool:
        """True when the printer currently has a print loaded / in progress.

        Returns False when disconnected or in any idle/terminal state
        (IDLE / FINISH / FAILED / unknown), so callers fail *open* only for
        the safe "nothing is printing" case. #1890.
        """
        state = self.get_status(printer_id)
        if not state or not state.connected:
            return False
        return state.state in self.ACTIVE_PRINT_STATES

    def get_model(self, printer_id: int) -> str | None:
        """Get the cached model for a printer."""
        return self._models.get(printer_id)

    def get_drying_targets(self, printer_id: int) -> dict[int, dict] | None:
        """Get cached active drying target params keyed by AMS id.

        Returned shape: ``{ams_id: {"filament": str, "temp": int}}``. Returns
        ``None`` when the printer is not connected. Seeded by
        ``send_drying_command(mode=1)`` and cleared when drying stops.
        """
        client = self._clients.get(printer_id)
        return client._drying_targets if client else None

    def get_all_statuses(self) -> dict[int, PrinterState]:
        """Get status of all connected printers (checks for stale connections)."""
        result = {}
        for printer_id, client in self._clients.items():
            # Check staleness and update connected state if needed
            client.check_staleness()
            result[printer_id] = client.state
        return result

    def is_connected(self, printer_id: int) -> bool:
        """Check if a printer is connected (checks for stale connections)."""
        if printer_id in self._clients:
            client = self._clients[printer_id]
            # Check staleness and update connected state if needed
            return client.check_staleness()
        return False

    def get_client(self, printer_id: int) -> BambuMQTTClient | None:
        """Get the MQTT client for a printer."""
        return self._clients.get(printer_id)

    async def ensure_fresh_connection(self, printer_id: int) -> bool:
        """Reconnect if MQTT connection exceeded the printer's mqtt_connection_timeout.

        Fetches the Printer from DB. Use ensure_fresh_connection_for_printer() if you already have it.
        Returns True if connection is fresh (or was refreshed), False if reconnect failed.
        """
        from backend.app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()

        if not printer:
            return False

        return await self.ensure_fresh_connection_for_printer(printer)

    async def ensure_fresh_connection_for_printer(self, printer: Printer) -> bool:
        """Reconnect if MQTT connection exceeded the printer's mqtt_connection_timeout.

        Use this when you already have the Printer ORM object to avoid an extra DB query.
        Returns True if connection is fresh (or was refreshed), False if reconnect failed.
        """
        printer_id = printer.id
        connected_at = self._connected_at.get(printer_id)
        if not connected_at:
            return printer_id in self._clients

        timeout = getattr(printer, "mqtt_connection_timeout", 0)
        if timeout <= 0:
            return True  # Timeout disabled

        elapsed = time.time() - connected_at
        if elapsed <= timeout:
            return True  # Connection still fresh

        logger.info(
            "MQTT connection stale for printer %s (%.0fs > %ds), reconnecting...",
            printer.name,
            elapsed,
            timeout,
        )
        ok = await self.connect_printer(printer)
        if not ok:
            # Background dispatch and most callers raise a generic "Can`t
            # re-connect printer MQTT" without printer context — surface
            # the printer name + IP here so the operator can match the
            # failure to a specific machine in the logs.
            logger.warning(
                "MQTT reconnect failed for printer %s (id=%s, ip=%s) after the connect-timeout poll",
                printer.name,
                printer.id,
                printer.ip_address,
            )
        return ok

    def mark_printer_offline(self, printer_id: int):
        """Mark a printer as offline and trigger status callback.

        This is used when we know the printer power was cut (e.g., smart plug turned off)
        to immediately update the UI without waiting for MQTT timeout.
        """
        import logging

        logger = logging.getLogger(__name__)

        if printer_id in self._clients:
            client = self._clients[printer_id]
            if client.state.connected:
                logger.info("Marking printer %s as offline (smart plug power off)", printer_id)
                client.state.connected = False
                client.state.state = "unknown"
                # Trigger the status change callback to broadcast via WebSocket
                if self._on_status_change:
                    self._schedule_async(self._on_status_change(printer_id, client.state))

    def start_print(
        self,
        printer_id: int,
        filename: str,
        plate_id: int = 1,
        ams_mapping: list[int] | None = None,
        bed_levelling: str | bool = True,
        flow_cali: str | bool = False,
        layer_inspect: bool = False,
        timelapse: bool = False,
        use_ams: bool = True,
        nozzle_offset_cali: str | bool = False,
        nozzle_mapping: str | None = None,
        nozzle_slot_extruders: str | None = None,
        storage: str = "external",
        file_md5: str = "",
        timelapse_storage: str | None = None,
    ) -> bool:
        """Start a print on a connected printer.

        ``storage`` says which medium the file was uploaded to and therefore
        which URL scheme the command carries; ``file_md5`` is the digest of the
        uploaded bytes, used only on the internal path. ``timelapse_storage``
        is a different question about a different medium — where the RECORDING
        goes — and the two are independent.

        ``nozzle_mapping`` is an opaque JSON string captured from BambuStudio's
        project_file MQTT command (H2C rack-swap slicer pick preservation,
        #1780). It rides through to the MQTT client untouched; the dispatch
        builder there parses + injects it only on dual-nozzle models.
        """
        caller = traceback.extract_stack(limit=3)[0]
        logger.info(
            "PRINT COMMAND: printer=%s, file=%s, caller=%s:%s:%s",
            printer_id,
            filename,
            caller.filename.split("/")[-1],
            caller.lineno,
            caller.name,
        )
        if printer_id in self._clients:
            return self._clients[printer_id].start_print(
                filename,
                plate_id,
                ams_mapping=ams_mapping,
                timelapse=timelapse,
                bed_levelling=bed_levelling,
                flow_cali=flow_cali,
                layer_inspect=layer_inspect,
                use_ams=use_ams,
                nozzle_offset_cali=nozzle_offset_cali,
                nozzle_mapping=nozzle_mapping,
                nozzle_slot_extruders=nozzle_slot_extruders,
                storage=storage,
                file_md5=file_md5,
                timelapse_storage=timelapse_storage,
            )
        return False

    async def execute_macro_and_wait(
        self,
        printer_id: int,
        gcode: str,
        macro_name: str,
    ) -> tuple[bool, str]:
        """Send a macro and block until ``on_macro_complete`` fires or the printer disconnects.

        Uses :func:`macro_executor.send_macro_and_await_ack` for the initial
        send+ACK, then waits for the ``stg_cur`` idle transition (reported by
        ``_broadcast_macro_complete``).  No fixed timeout — the printer's own
        status tracking handles errors/stalls.  A connectivity health-check
        every 0.5 s catches disconnects that wouldn't trigger a callback.

        A disconnect is tolerated for :data:`MACRO_DISCONNECT_GRACE_SECONDS`.
        It used to end the wait on the first offline poll, which turned an
        ordinary MQTT blip into "Swap macro 'Start Sequence' failed: Printer
        disconnected during macro execution" — reported from a farm whose log
        carries 11 disconnects and 20 reconnects in 9h24m. A swap sequence runs
        for tens of seconds, so the window was wide open, and the resulting
        failed print armed the plate-clear gate and stalled the queue.

        If contact never returns the wait still stops — proceeding without
        knowing whether the bed was prepared is worse than a false failure — but
        the message says the macro's state is *unknown*, not that it failed, so
        the operator is pointed at the network rather than at their macro.

        Returns ``(success, message)``.
        """
        from backend.app.services.macro_executor import send_macro_and_await_ack

        client = self._clients.get(printer_id)
        if not client:
            return False, "Printer not connected"

        model = self._models.get(printer_id)
        ack_ok, ack_msg = await send_macro_and_await_ack(client, gcode, macro_name, model)
        if not ack_ok:
            return False, ack_msg

        # Register a completion waiter. _broadcast_macro_complete will .set()
        # the Event when bambu_mqtt fires on_macro_complete.
        event = asyncio.Event()
        result: dict = {"status": "pending", "message": ""}
        self._macro_waiters[printer_id] = (event, result)

        offline_since: float | None = None
        watched = client  # identity, so a swap can be named rather than guessed at
        try:
            while not event.is_set():
                # Re-read every poll instead of watching the captured `client`.
                # ``connect_printer`` does not mutate a client, it REPLACES the
                # entry in ``self._clients`` — so a reference captured before the
                # loop is orphaned by any reconnect and reports disconnected for
                # ever, whatever the real printer is doing.
                #
                # That made this grace period unsatisfiable rather than generous:
                # measured on a farm's log, five macro waits went offline, five
                # gave up at the limit, and not one ever recovered. All five began
                # within half a second of BamDude's own staleness reconnect —
                # ``ensure_fresh_connection_for_printer``, called from dispatch and
                # the scheduler, which recycles a link older than
                # ``mqtt_connection_timeout``. So the dispatcher, placing the next
                # job, was killing the macro still running on that same printer,
                # and no length of grace could have helped: the object being
                # watched was already dead.
                #
                # The completion side was always reconnect-safe — the waiter is
                # keyed by printer_id and ``on_macro_complete`` is re-bound to the
                # new client — so this poll was the only thing holding a stale
                # reference.
                live = self._clients.get(printer_id)

                if live is not None and live is not watched:
                    logger.info(
                        "[MACRO-WAIT] Printer %s reconnected while macro '%s' was running — following the new "
                        "connection",
                        printer_id,
                        macro_name,
                    )
                    watched = live

                if live is not None and live.state.connected:
                    if offline_since is not None:
                        logger.info(
                            "[MACRO-WAIT] Printer %s came back after %.1fs offline — still waiting for macro '%s'",
                            printer_id,
                            asyncio.get_running_loop().time() - offline_since,
                            macro_name,
                        )
                        offline_since = None
                else:
                    now = asyncio.get_running_loop().time()
                    if offline_since is None:
                        offline_since = now
                        logger.warning(
                            "[MACRO-WAIT] Printer %s went offline while running macro '%s' — "
                            "holding for up to %.0fs in case it reconnects",
                            printer_id,
                            macro_name,
                            MACRO_DISCONNECT_GRACE_SECONDS,
                        )
                    elif now - offline_since >= MACRO_DISCONNECT_GRACE_SECONDS:
                        logger.error(
                            "[MACRO-WAIT] Printer %s stayed offline for %.0fs during macro '%s' — "
                            "giving up; whether the macro ran is unknown",
                            printer_id,
                            now - offline_since,
                            macro_name,
                        )
                        # Deliberately not "the macro failed": we do not know
                        # that. The printer may well have run it. Naming the
                        # cause sends the operator to the network instead of to
                        # their macro definition.
                        return False, (
                            "Lost contact with the printer while the macro was running, and it did not come "
                            f"back within {MACRO_DISCONNECT_GRACE_SECONDS:.0f}s — whether the macro ran is unknown"
                        )
                await asyncio.sleep(0.5)
        finally:
            self._macro_waiters.pop(printer_id, None)

        # The completion event is the authority, not the socket: a printer that
        # reports the macro done and drops immediately after has still done it.
        return result["status"] == "completed", result.get("message", "")

    def stop_print(self, printer_id: int) -> bool:
        """Stop the current print on a connected printer."""
        if printer_id in self._clients:
            return self._clients[printer_id].stop_print()
        return False

    async def wait_for_cooldown(
        self,
        printer_id: int,
        target_temp: float = 50.0,
        timeout: int = 600,
        check_interval: int = 10,
    ) -> bool:
        """Wait for the nozzle to cool down to a safe temperature.

        Args:
            printer_id: The printer to monitor
            target_temp: Target temperature to wait for (default 50°C)
            timeout: Maximum seconds to wait (default 600s = 10 min)
            check_interval: Seconds between temperature checks (default 10s)

        Returns:
            True if cooled down, False if timeout or not connected
        """
        import logging

        logger = logging.getLogger(__name__)

        elapsed = 0
        while elapsed < timeout:
            state = self.get_status(printer_id)
            if not state or not state.connected:
                logger.warning("Printer %s disconnected during cooldown wait", printer_id)
                return False

            # Check nozzle temperature (and nozzle_2 for dual extruders)
            nozzle_temp = state.temperatures.get("nozzle", 0)
            nozzle_2_temp = state.temperatures.get("nozzle_2", 0)
            max_temp = max(nozzle_temp, nozzle_2_temp)

            if max_temp <= target_temp:
                logger.info("Printer %s cooled down to %s°C", printer_id, max_temp)
                return True

            logger.debug("Printer %s nozzle at %s°C, waiting for %s°C...", printer_id, max_temp, target_temp)
            await asyncio.sleep(check_interval)
            elapsed += check_interval

        logger.warning("Printer %s cooldown timeout after %ss", printer_id, timeout)
        return False

    def send_drying_command(
        self,
        printer_id: int,
        ams_id: int,
        temp: int,
        duration: int,
        mode: int = 1,
        filament: str = "",
        rotate_tray: bool = False,
    ) -> bool:
        """Send AMS drying command to printer."""
        if printer_id not in self._clients:
            return False
        return self._clients[printer_id].send_drying_command(ams_id, temp, duration, mode, filament, rotate_tray)

    def request_status_update(self, printer_id: int) -> bool:
        """Request a full status update from the printer.

        This sends a 'pushall' command to get the latest data including nozzle info.
        """
        if printer_id in self._clients:
            return self._clients[printer_id].request_status_update()
        return False

    # Probe budget for test_connection (#1445). Was a fixed 2s sleep, which was
    # too short for P1S firmware whose broker / TLS handshake routinely takes
    # 3–5s to surface a CONNACK on a cold MQTT session. We now poll up to
    # PROBE_TIMEOUT_SECONDS and early-return the moment we see connected=True,
    # so happy-path connections still finish in ~1–2s and slow brokers get the
    # headroom they need instead of getting falsely rejected.
    PROBE_TIMEOUT_SECONDS = 8.0
    PROBE_POLL_INTERVAL_SECONDS = 0.2

    async def test_connection(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str,
    ) -> dict:
        """Test connection to a printer without persisting.

        Polls for up to PROBE_TIMEOUT_SECONDS and tears the probe client down
        off-loop. The teardown matters: `client.disconnect()` ends in paho's
        `loop_stop()` which `join()`s the network thread — if the thread is
        still mid-TLS-handshake to a slow printer, that join blocks the
        asyncio event loop and every other HTTP request queues behind it. The
        original synchronous teardown produced the #1445 "Docker container
        hangs" symptom on P1S when called from POST /printers/.
        """
        client = BambuMQTTClient(
            ip_address=ip_address,
            serial_number=serial_number,
            access_code=access_code,
        )

        try:
            client.connect()
            deadline = asyncio.get_running_loop().time() + self.PROBE_TIMEOUT_SECONDS
            while not client.state.connected and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(self.PROBE_POLL_INTERVAL_SECONDS)

            result = {
                "success": client.state.connected,
                "state": client.state.state if client.state.connected else None,
                "model": client.state.raw_data.get("device_model"),
            }
        finally:
            # Off-loop teardown — see docstring. paho's loop_stop() joins the
            # network thread which may still be in a slow TLS handshake.
            await asyncio.to_thread(client.disconnect)

        return result

    async def _broadcast_macro_complete(self, printer_id: int, macro_name: str, status: str):
        """Notify waiting dispatch pipeline, then broadcast via WebSocket."""
        # Unblock the dispatch pipeline first — it's blocking on the Event.
        waiter = self._macro_waiters.get(printer_id)
        if waiter:
            event, result = waiter
            result["status"] = status
            result["message"] = f"Macro '{macro_name}' {status}"
            event.set()

        from backend.app.core.websocket import ws_manager

        printer_name = self._printer_info.get(printer_id)
        await ws_manager.broadcast(
            {
                "type": "macro_executed",
                "data": {
                    "printer_id": printer_id,
                    "printer_name": printer_name.name if printer_name else str(printer_id),
                    "macro_name": macro_name,
                    "status": status,
                    "success": status == "completed",
                    "message": f"Macro '{macro_name}' {status}",
                },
            }
        )


def get_derived_status_name(state: PrinterState, model: str | None = None) -> str | None:
    """
    Compute a human-readable status name based on printer state.

    Uses stg_cur when available, otherwise derives status from temperature data
    when the printer is heating before a print starts.

    Args:
        state: The printer state to analyze
        model: Optional printer model for model-specific workarounds
    """
    # Macro executing - show macro name instead of default "Printing" text
    if state.macro_executing and state.stg_cur == 0:
        return f"Executing: {state.macro_executing}"

    # A1/A1 Mini firmware bug: some versions report stg_cur=0 when idle
    # Only correct this specific case (IDLE + stg_cur=0) for affected models
    if state.state == "IDLE" and state.stg_cur == 0 and has_stg_cur_idle_bug(model):
        return None

    # If we have a valid calibration stage, use it
    # X1 models use -1 for idle, A1/P1 models use 255 for idle
    # Valid stage numbers are 0-254
    if 0 <= state.stg_cur < 255:
        return get_stage_name(state.stg_cur)

    # If not in RUNNING state, no derived status needed
    if state.state != "RUNNING":
        return None

    # Check if we're in an early phase where temperatures are heating
    temps = state.temperatures or {}
    progress = state.progress or 0

    # Only derive heating status when progress is very low (< 2%)
    # This indicates we're in the preparation phase, not actually printing
    if progress >= 2:
        return None

    # Check bed temperature - if target is set and current is significantly below
    bed_temp = temps.get("bed", 0)
    bed_target = temps.get("bed_target", 0)

    # Check nozzle temperature
    nozzle_temp = temps.get("nozzle", 0)
    nozzle_target = temps.get("nozzle_target", 0)

    # Temperature thresholds: consider "heating" if more than 10°C below target
    TEMP_THRESHOLD = 10

    # Determine what's heating (prioritize bed since it takes longer)
    if bed_target > 30 and (bed_target - bed_temp) > TEMP_THRESHOLD:
        return "Heating heatbed"
    elif nozzle_target > 30 and (nozzle_target - nozzle_temp) > TEMP_THRESHOLD:
        return "Heating nozzle"

    # If targets are set but we're close to them, we might be in final prep
    if bed_target > 30 or nozzle_target > 30:
        if progress == 0 and state.layer_num == 0:
            return "Preparing"

    return None


_PLATE_ID_RE = re.compile(r"plate_(\d+)\.gcode")


def parse_plate_id(gcode_file: str | None) -> int | None:
    """Extract the 1-indexed plate number from a Bambu gcode_file path.

    Returns None when the path is missing or has no ``plate_N.gcode`` segment.
    Shared by the REST status route and the WebSocket push path so both agree
    on the value sent to the frontend (upstream #881 follow-up).
    """
    if not gcode_file:
        return None
    match = _PLATE_ID_RE.search(gcode_file)
    return int(match.group(1)) if match else None


def resolve_plate_id(state: PrinterState) -> int | None:
    """Resolve the active plate number from a PrinterState (#1166).

    Some firmware versions (e.g. P1S 01.10.00.00) put only the .3mf filename
    in ``print.gcode_file``, so :func:`parse_plate_id` returns None and the
    printer card / cover endpoint falls back to plate 1 — wrong thumbnail
    on multi-plate prints. When BamDude dispatched the print itself we
    already know the right plate, so we prefer that over the gcode_file
    echo. The subtask check prevents stale values from a previous
    BamDude-dispatched print bleeding into a Studio-direct print on the
    same printer.
    """
    dispatched_plate = getattr(state, "dispatched_plate_id", None)
    dispatched_subtask = getattr(state, "dispatched_subtask", None)
    if (
        dispatched_plate is not None
        and dispatched_subtask is not None
        and state.subtask_name
        and dispatched_subtask == state.subtask_name
    ):
        return dispatched_plate
    return parse_plate_id(state.gcode_file)


def resolve_expected_tray(
    raw_slot: int | None,
    ams_layout: list[tuple[int, bool]],
    mapping_raw: object,
) -> int | None:
    """Globalise a raw firmware ``tray_tar``/``tray_pre`` value for the runout UI.

    The firmware reports the target/previous slot as a bare number whose meaning
    depends on the AMS layout (see ``PrinterState.tray_tar``). This mirrors the
    ``tray_now`` handling so the resolved ID lines up with what the AMS graphic
    already highlights via ``ams_id*4 + slot`` (upstream #2587).

    ``ams_layout`` is a list of ``(ams_id, is_ams_ht)`` for the connected units.

    - ``255``/``-1`` (none/idle) → ``None``
    - ``254`` (external spool) → ``254``
    - ``128``-``135`` (AMS-HT) → already global, returned as-is
    - ``0``-``3`` local slot:
        * exactly one regular AMS → ``ams_id*4 + slot``
        * several regular AMS → resolved via the snow-encoded ``mapping`` field
          (each entry = ``ams_hw_id*256 + slot``; ``65535`` = unmapped), or
          ``None`` when it stays ambiguous (honest "can't determine")
        * no regular AMS → ``None``
    - ``4``-``15`` → already a global regular-AMS ID, returned as-is

    Returns ``None`` for anything it can't place, so the caller surfaces a
    "check the printer" message instead of pointing at the wrong slot.
    """
    if raw_slot is None or raw_slot in (255, -1):
        return None
    if raw_slot == 254:
        return 254
    if 128 <= raw_slot <= 135:
        return raw_slot
    if 0 <= raw_slot <= 3:
        regular = [ams_id for ams_id, is_ht in ams_layout if not is_ht]
        if len(regular) == 1:
            return regular[0] * 4 + raw_slot
        if len(regular) > 1:
            if not isinstance(mapping_raw, list):
                return None
            candidates: set[int] = set()
            for value in mapping_raw:
                if not isinstance(value, int) or value >= 65535:
                    continue
                ams_hw_id = value >> 8
                slot = value & 0xFF
                if 0 <= ams_hw_id <= 3 and (slot & 0x03) == raw_slot:
                    candidates.add(ams_hw_id * 4 + raw_slot)
                elif 128 <= ams_hw_id <= 135 and raw_slot == 0:
                    candidates.add(ams_hw_id)
            return candidates.pop() if len(candidates) == 1 else None
        return None
    if 4 <= raw_slot <= 15:
        return raw_slot
    # 24-27 = A2L AMS-Lite (normalised unit 6) global tray ids, already resolved.
    if 24 <= raw_slot <= 27:
        return raw_slot
    return None


# Moved here from the status route: the WebSocket payload needs the same
# list, and the card renders its fans from nothing else. Leaving it in the
# route meant a fan speed reached the browser only on the next poll.
def _airduct_fans(model: str | None, state) -> list[AirductFan]:
    """The fans this printer reports through ``device.airduct``, named (#2576).

    Presence in ``airduct_parts`` is the hardware check — the printer lists only
    fitted parts, which matters on the P2S where the second auxiliary fan and
    the exhaust fan are both add-on kits.

    The label comes from the mirrored per-model config and depends on the
    airduct mode, because the same part id is a different fan on different
    models: part 10 is the LEFT aux on a P2S and the RIGHT one on an X2D. See
    ``printer_configs.airduct_fan_label``.

    Sorted by part id so the badges keep a stable order across pushes rather
    than following dict insertion, which follows whatever order the printer
    happened to send.
    """
    fans: list[AirductFan] = []
    mode_id = airduct_mode_effective(state)
    for part_id, part in sorted(airduct_parts_effective(state, model).items()):
        # Air doors are in the same list (type 1) and are not fans.
        if part.get("type") not in (0, None):
            continue
        # The effective mode, not the raw one: an old-protocol printer keys
        # its fan names under "-1", which is what converse_to_duct stamps.
        label_key, label = airduct_fan_label(model, mode_id, state.airduct_sub_mode, part_id)
        control = airduct_fan_control(state, part_id)
        fans.append(
            AirductFan(
                part_id=part_id,
                speed=int(part.get("state", 0)),
                range_start=int(part.get("range_start", 0)),
                range_end=int(part.get("range_end", 100)),
                control=control,
                controllable=control == FAN_CTRL,
                label_key=label_key,
                label=label,
            )
        )
    return fans


def printer_state_to_dict(
    state: PrinterState,
    printer_id: int | None = None,
    model: str | None = None,
    drying_targets: dict[int, dict] | None = None,
) -> dict:
    """Convert PrinterState to a JSON-serializable dict.

    Args:
        state: The printer state to convert
        printer_id: Optional printer ID for generating cover URLs
        model: Optional printer model for filtering unsupported features
        drying_targets: Optional per-AMS active-cycle params
            (``{ams_id: {"filament": str, "temp": int}}``) from the
            BambuMQTTClient cache so the badge can display "PETG @ 65°C".
    """
    # Parse AMS data from raw_data
    ams_units = []
    vt_tray = []
    raw_data = state.raw_data or {}

    # Build K-profile lookup map: cali_idx -> k_value
    kprofile_map: dict[int, float] = {}
    for kp in state.kprofiles or []:
        if kp.slot_id is not None and kp.k_value:
            try:
                kprofile_map[kp.slot_id] = float(kp.k_value)
            except (ValueError, TypeError):
                pass  # Skip K-profile entries with unparseable values

    if "ams" in raw_data and isinstance(raw_data["ams"], list):
        for ams_data in raw_data["ams"]:
            trays = []
            for tray in ams_data.get("tray", []):
                tag_uid = tray.get("tag_uid")
                if tag_uid in ("", "0000000000000000"):
                    tag_uid = None
                tray_uuid = tray.get("tray_uuid")
                if tray_uuid in ("", "00000000000000000000000000000000"):
                    tray_uuid = None

                # Get K value: first try tray's k field, then lookup from K-profiles
                k_value = tray.get("k")
                cali_idx = tray.get("cali_idx")
                if k_value is None and cali_idx is not None and cali_idx in kprofile_map:
                    k_value = kprofile_map[cali_idx]

                # P1S / A1 Mini physically-empty-slot signal (#1322
                # follow-up): for a truly empty slot the firmware sends
                # only ``{"id": N}`` — no state, no tray_type, no anything
                # else. Treat that as the firmware's "no spool" indicator
                # (state=9) so the assign-spool path in inventory.py can
                # short-circuit a MQTT publish the firmware would silently
                # drop anyway. The post-"Reset Slot" A1 Mini BMCU case
                # sends a populated payload (state=3, tray_type="") —
                # different shape, doesn't match this guard, still
                # attempts the MQTT push per the #1322 root fix. Steady-
                # state populated-payload empty signal is handled at the
                # MQTT-merge layer in ``bambu_mqtt`` via
                # ``tray_exist_bits``; this stays as belt-and-suspenders
                # for paths that skip that merge.
                state_val = tray.get("state")
                if state_val is None and len(tray) == 1 and "id" in tray:
                    state_val = 9

                trays.append(
                    {
                        "id": int(tray.get("id", 0)),
                        "tray_color": tray.get("tray_color"),
                        "tray_type": tray.get("tray_type"),
                        "tray_sub_brands": tray.get("tray_sub_brands"),
                        "tray_id_name": tray.get("tray_id_name"),
                        "tray_info_idx": tray.get("tray_info_idx"),
                        "remain": tray.get("remain", 0),
                        "k": k_value,
                        "cali_idx": cali_idx,
                        "tag_uid": tag_uid,
                        "tray_uuid": tray_uuid,
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "drying_temp": tray.get("drying_temp"),
                        "drying_time": tray.get("drying_time"),
                        "state": state_val,
                        # Firmware's authoritative "spool physically present" bit
                        # (tray_exist_bits, upstream #2527). Upstream only wired
                        # this into the REST payload; we must emit it here too —
                        # the WS status merges wholesale into the printerStatus
                        # query cache, so an `ams` array without `exists` would
                        # drop the flag again on the very next push.
                        "exists": tray.get("exists"),
                    }
                )
            # Prefer humidity_raw (actual percentage) over humidity (index 1-5)
            humidity_raw = ams_data.get("humidity_raw")
            humidity_idx = ams_data.get("humidity")
            humidity_value = None

            if humidity_raw is not None:
                try:
                    humidity_value = int(humidity_raw)
                except (ValueError, TypeError):
                    pass  # Skip unparseable humidity; will try index fallback
            # Fall back to index if no raw value (index is 1-5, not percentage)
            if humidity_value is None and humidity_idx is not None:
                try:
                    humidity_value = int(humidity_idx)
                except (ValueError, TypeError):
                    pass  # Skip unparseable humidity index; humidity remains None

            # AMS-HT has 1 tray, regular AMS has 4 trays
            is_ams_ht = len(trays) == 1

            # Active-cycle filament + target temperature for the drying badge.
            # Bambu doesn't echo the chosen filament/temp on the per-tick AMS
            # push, so prefer the cached target from the last send_drying_command;
            # fall back to the loaded trays, but only when they agree on a
            # filament type — see uniform_tray_drying_hint.
            ams_id_int = int(ams_data.get("id", 0))
            target = (drying_targets or {}).get(ams_id_int)
            dry_target_temp: int | None = None
            dry_filament: str | None = None
            if target:
                temp_val = target.get("temp")
                fil_val = target.get("filament") or ""
                if temp_val is not None:
                    try:
                        dry_target_temp = int(temp_val)
                    except (TypeError, ValueError):
                        dry_target_temp = None
                if fil_val:
                    dry_filament = str(fil_val)
            if dry_target_temp is None or not dry_filament:
                hint_filament, hint_temp = uniform_tray_drying_hint(
                    [(tray.get("tray_type") or "", tray.get("drying_temp")) for tray in trays]
                )
                if not dry_filament:
                    dry_filament = hint_filament
                if dry_target_temp is None:
                    dry_target_temp = hint_temp

            ams_units.append(
                {
                    "id": ams_id_int,
                    "humidity": humidity_value,
                    "temp": ams_data.get("temp"),
                    "is_ams_ht": is_ams_ht,
                    "tray": trays,
                    # Serial number: Bambu MQTT uses "sn" key on AMS unit objects
                    "serial_number": str(ams_data.get("sn") or ams_data.get("serial_number") or ""),
                    # Firmware version: populated by _handle_version_info from get_version
                    "sw_ver": str(ams_data.get("sw_ver") or ""),
                    # Drying: dry_time > 0 means drying is active (minutes remaining)
                    "dry_time": int(ams_data.get("dry_time") or 0),
                    # Drying status from info hex bits (0=Off, 1=Checking, 2=Drying, 3=Cooling, etc.)
                    "dry_status": int(ams_data.get("dry_status") or 0),
                    "dry_sub_status": int(ams_data.get("dry_sub_status") or 0),
                    # Cannot-dry reasons from firmware (e.g. 1=InsufficientPower, 8=NeedPluginPower)
                    "dry_sf_reason": list(ams_data.get("dry_sf_reason") or []),
                    # Active-cycle filament name + target temperature for the badge
                    "dry_target_temp": dry_target_temp,
                    "dry_filament": dry_filament,
                    # Module type: "ams", "n3f", "n3s" (from get_version)
                    "module_type": str(ams_data.get("module_type") or ""),
                }
            )

    # Parse virtual tray (external spool) - now a list
    if "vt_tray" in raw_data:
        vt_tray_raw = raw_data["vt_tray"]
        if isinstance(vt_tray_raw, dict):
            vt_tray_raw = [vt_tray_raw]
        elif not isinstance(vt_tray_raw, list):
            vt_tray_raw = []
        for vt_data in vt_tray_raw:
            vt_tag_uid = vt_data.get("tag_uid")
            if vt_tag_uid in ("", "0000000000000000"):
                vt_tag_uid = None
            vt_tray_uuid = vt_data.get("tray_uuid")
            if vt_tray_uuid in ("", "00000000000000000000000000000000"):
                vt_tray_uuid = None

            # Get K value for vt_tray
            vt_k_value = vt_data.get("k")
            vt_cali_idx = vt_data.get("cali_idx")
            if vt_k_value is None and vt_cali_idx is not None and vt_cali_idx in kprofile_map:
                vt_k_value = kprofile_map[vt_cali_idx]

            tray_id = int(vt_data.get("id", 254))
            vt_tray.append(
                {
                    "id": tray_id,
                    "tray_color": vt_data.get("tray_color"),
                    "tray_type": vt_data.get("tray_type"),
                    "tray_sub_brands": vt_data.get("tray_sub_brands"),
                    "tray_id_name": vt_data.get("tray_id_name"),
                    "tray_info_idx": vt_data.get("tray_info_idx"),
                    "remain": vt_data.get("remain", 0),
                    "k": vt_k_value,
                    "cali_idx": vt_cali_idx,
                    "tag_uid": vt_tag_uid,
                    "tray_uuid": vt_tray_uuid,
                    "nozzle_temp_min": vt_data.get("nozzle_temp_min"),
                    "nozzle_temp_max": vt_data.get("nozzle_temp_max"),
                }
            )

    # Get ams_extruder_map from raw_data (populated by MQTT handler from AMS info field)
    ams_extruder_map = raw_data.get("ams_extruder_map", {})

    # Filter out chamber temp for models that don't have a real sensor
    # P1P, P1S, A1, A1Mini report meaningless chamber_temper values
    temperatures = state.temperatures
    if not supports_chamber_temp(model):
        temperatures = {
            k: v for k, v in temperatures.items() if k not in ("chamber", "chamber_target", "chamber_heating")
        }

    result = {
        "connected": state.connected,
        "state": state.state,
        "current_print": state.current_print,
        "subtask_name": state.subtask_name,
        "gcode_file": state.gcode_file,
        "progress": state.progress,
        "remaining_time": state.remaining_time,
        "layer_num": state.layer_num,
        "total_layers": state.total_layers,
        "temperatures": temperatures,
        # Same shape as the REST HMSErrorResponse — the HMS modal renders its
        # action buttons (and submits them with full_code/job_id) from whichever
        # payload arrived last. The WS dict used to drop these three fields, so
        # an open dialog lost its Done/Retry buttons on the first live push.
        "hms_errors": [
            {
                "code": e.code,
                "attr": e.attr,
                "module": e.module,
                "severity": e.severity,
                "actions": e.actions,
                "full_code": e.full_code,
                "job_id": e.job_id,
            }
            for e in (state.hms_errors or [])
        ],
        # Pause classification — populated by main._handle_pause_edge, cleared
        # by _handle_resume_edge. ``pause_reason`` is the normalised key
        # (user / filament_runout / door_open / presence_check /
        # file_pause_command / ai_first_layer_defect / ai_spaghetti /
        # foreign_object / plate_objects / hms_other / unknown) for routing /
        # filtering on the frontend; ``pause_reason_label`` is the
        # operator-facing copy (precise HMS description when available, else
        # generic PAUSE_REASON_LABELS entry); ``pause_started_at`` is the
        # epoch-seconds wall-clock at which the pause began so the live
        # counter ("Paused 14 min") survives an F5.
        "pause_reason": state.pause_reason,
        "pause_reason_label": state.pause_reason_label,
        "pause_started_at": state.pause_started_at,
        # AMS data for filament colors
        "ams": ams_units if ams_units else None,
        "vt_tray": vt_tray,
        # AMS status for filament change tracking
        "ams_status_main": state.ams_status_main,
        "ams_status_sub": state.ams_status_sub,
        "tray_now": state.tray_now,
        # Runout / filament-replacement guidance (upstream #2587). Only meaningful
        # while PAUSED — resolve the firmware's target/previous slot to a global
        # tray ID so the AMS graphic can highlight the slot the print now expects
        # and name the one that ran out. None when idle, not paused, or unresolvable.
        "expected_tray": (
            resolve_expected_tray(
                state.tray_tar,
                [(u["id"], u.get("is_ams_ht", False)) for u in ams_units],
                raw_data.get("mapping"),
            )
            if state.state == "PAUSE"
            else None
        ),
        "previous_tray": (
            resolve_expected_tray(
                state.tray_pre,
                [(u["id"], u.get("is_ams_ht", False)) for u in ams_units],
                raw_data.get("mapping"),
            )
            if state.state == "PAUSE"
            else None
        ),
        # AMS Filament Backup (auto_switch_filament): True/False/None (#1766)
        "ams_auto_switch_filament": state.ams_auto_switch_filament,
        # Per-AMS extruder map: {ams_id: extruder_id} where 0=right, 1=left
        "ams_extruder_map": ams_extruder_map,
        # WiFi signal strength
        "wifi_signal": state.wifi_signal,
        "wired_network": state.wired_network,
        # Calibration stage tracking
        "stg_cur": state.stg_cur,
        "stg_cur_name": get_derived_status_name(state, model),
        # Printable objects count for skip objects feature
        "printable_objects_count": len(state.printable_objects),
        "skip_objects_supported": state.skip_objects_supported,
        # Fan speeds (0-100 percentage, None if not available)
        "cooling_fan_speed": state.cooling_fan_speed,
        "big_fan1_speed": state.big_fan1_speed,
        "big_fan2_speed": state.big_fan2_speed,
        "heatbreak_fan_speed": state.heatbreak_fan_speed,
        # Chamber light state
        "chamber_light": state.chamber_light,
        # The air duct, so a mode change lands on the card as soon as the
        # printer confirms it rather than at the next poll. ⚠️ A field the REST
        # status serves but this dict omits updates only by refetch — see L14 in
        # the printer-control registry for the eleven others still in that state.
        # The rest of what the card renders live. Each of these used to reach the
        # browser only on the next refetch, because this dict is a hand-kept
        # projection and they were never added to it — see L14 in the
        # printer-control registry, and the test that now fails on the next
        # omission.
        "firmware_consistency_request": state.firmware_consistency_request,
        "firmware_force_upgrade": state.firmware_force_upgrade,
        "speed_level": state.speed_level,
        "door_open": state.door_open,
        "sdcard": state.sdcard,
        "sdcard_state": state.sdcard_state,
        # One composed answer about storage, so the file browser's switcher and
        # the dispatcher's transport choice cannot drift apart. See
        # utils/printer_storage.py — it is the same discipline as
        # utils/timelapse.py::capability_for.
        "storage_capability": storage_capability_for(model, state),
        "store_to_sdcard": state.store_to_sdcard,
        "timelapse": state.timelapse,
        "ipcam": state.ipcam,
        "firmware_version": state.firmware_version,
        "stg": state.stg,
        "mc_print_sub_stage": state.mc_print_sub_stage,
        "last_ams_update": state.last_ams_update,
        # What the heaters will accept, so the UI can bound its inputs off the
        # same rule the backend clamps with instead of a second copy of it.
        "temperature_limits": {k: list(v) for k, v in limits_for(model, state).items()},
        # Which extruders report a hotend fitted. Absent = the machine cannot
        # tell, which is NOT the same as "none fitted" — see ``ext_has_nozzle``.
        "ext_has_nozzle": dict(state.ext_has_nozzle),
        "supports_chamber_heater": supports_chamber_heater(model),
        "axis_at_home": dict(state.axis_at_home),
        "ext_has_filament": dict(state.ext_has_filament),
        "timelapse_capability": timelapse_capability_for(model, state),
        "airduct_fans": [f.model_dump() for f in _airduct_fans(model, state)],
        "airduct_mode": state.airduct_mode,
        "airduct_sub_mode": state.airduct_sub_mode,
        # Active extruder for dual-nozzle printers (0=right, 1=left)
        "active_extruder": state.active_extruder,
        # H2C nozzle rack (tool-changer dock positions)
        # Map raw MQTT field names (type/diameter) to schema names (nozzle_type/nozzle_diameter)
        "nozzle_rack": [
            {
                "id": n.get("id", 0),
                "nozzle_type": n.get("type", ""),
                "nozzle_diameter": n.get("diameter", ""),
                "wear": n.get("wear"),
                "stat": n.get("stat"),
                "max_temp": n.get("max_temp", 0),
                "serial_number": n.get("serial_number", ""),
                "filament_color": n.get("filament_color", ""),
                "filament_id": n.get("filament_id", ""),
            }
            for n in (state.nozzle_rack or [])
        ],
        # AMS drying support
        "supports_drying": supports_drying(model, state.firmware_version),
        "supports_drying_while_printing": supports_drying_while_printing(model, state.firmware_version),
        # AMS can dry but only from the printer's own screen (P1 series, #2533).
        "drying_screen_only": drying_screen_only(model),
        # 1-indexed plate number from the active print. Resolution order
        # (see resolve_plate_id docstring for the rationale): BamDude-dispatched
        # plate (when the subtask matches) → ``plate_N.gcode`` regex on
        # ``state.gcode_file``. The fallback covers firmware revisions
        # (P1S 01.10.00.00 etc.) that only echo the .3mf filename without the
        # plate path — without resolve_plate_id those installs always reported
        # plate 1 (#1166). Pushed via WebSocket so the printer card picks up
        # plate transitions in a multi-plate 3MF without waiting for the 30 s
        # REST poll (upstream #881 follow-up). current_archive_id is
        # intentionally REST-only — stable for the life of a print and needs
        # a DB lookup the WS path shouldn't pay for.
        "current_plate_id": resolve_plate_id(state),
        # Queue plate-clear gate (#961): surfaces the same value the REST /status
        # route returns so frontend WS merge reflects transitions within ~100 ms
        # instead of waiting for the 30 s REST poll — without this the "Mark plate
        # as cleared" button appeared 30 s–5 min late (upstream #939 follow-up).
        # Sourced from the authoritative in-memory + DB-backed accessor on the
        # manager singleton, which mirrors print_queue.awaiting_plate_clear (m010).
        "awaiting_plate_clear": (
            printer_manager.is_awaiting_plate_clear(printer_id) if printer_id is not None else False
        ),
    }
    # Add cover URL if there's an active print and printer_id is provided
    # Include PAUSE state so skip objects modal can show cover
    if printer_id and state.state in ("RUNNING", "PAUSE") and state.gcode_file:
        result["cover_url"] = f"/api/v1/printers/{printer_id}/cover"
    else:
        result["cover_url"] = None
    return result


# Global printer manager instance
printer_manager = PrinterManager()


async def init_printer_connections(db: AsyncSession):
    """Initialize connections to all active printers."""
    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False)))
    printers = result.scalars().all()

    for printer in printers:
        await printer_manager.connect_printer(printer)
