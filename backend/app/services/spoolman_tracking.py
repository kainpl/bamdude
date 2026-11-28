"""Spoolman per-filament usage tracking for active prints.

Captures AMS tray state and G-code data at print start, then reports
per-filament usage to the correct Spoolman spools at print completion.
Supports accurate partial usage reporting for failed/cancelled prints.
"""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select

from backend.app.core.config import settings as app_settings
from backend.app.core.database import async_session
from backend.app.services import spoolman_usage_history
from backend.app.services.print_usage_journal import without_stale_refill_runouts
from backend.app.services.spoolman import (
    SpoolmanClientError,
    SpoolmanNotFoundError,
    SpoolmanUnavailableError,
)
from backend.app.services.spoolman_spool_identity import FROM_ROWS, ChargeCheck
from backend.app.services.spoolman_weights import spool_net_weight, spool_price_per_gram
from backend.app.utils.filament_remaining import usable_remain_percent
from backend.app.utils.tray_ids import (
    external_slot_index,
    global_tray_id as stored_global_tray,
    is_external_id,
    names_loaded_source,
    slot_key as tray_slot_key,
    state_is_direct_rack,
    unit_type,
)
from backend.app.utils.tray_split import CompletionSnapshot, PrintedBound, printed_bound, sole_owner, split_weight

logger = logging.getLogger(__name__)

# Zero UUID used by Bambu printers for empty/unset tray_uuid
_ZERO_UUID = "00000000000000000000000000000000"
_ZERO_TAG_UID = "0000000000000000"


def _is_non_zero_identifier(value: str) -> bool:
    """Return True when identifier is non-empty and not all zeros."""
    if not value:
        return False
    return set(value) != {"0"}


def _to_fixed_hex(value: int, width: int) -> str:
    """Mirror frontend toFixedHex(): uppercase, zero-padded, fixed width."""
    safe = max(0, int(value))
    return format(safe, "X").zfill(width)[-width:]


def _hash_serial_to_hex32(serial: str) -> str:
    """Mirror frontend hashSerialToHex32() exactly (32-bit FNV-1a)."""
    input_str = (serial or "").strip().upper()
    hash_value = 0x811C9DC5
    for char in input_str:
        hash_value ^= ord(char)
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return format(hash_value, "X").zfill(8)


def _every_global_tray(tray_remain_start: dict | None, ams_trays: dict | None) -> set[int]:
    """Every global tray id the remain-delta fallback could charge: the start snapshot's slots
    (``"<ams>-<tray>"``, encoded as ``build_ams_tray_lookup`` does) and the trays seen."""
    ids = {tray for tray in (ams_trays or {}) if isinstance(tray, int)}
    for slot_key in tray_remain_start or {}:
        try:
            ams_id, tray_id = (int(part) for part in str(slot_key).split("-", 1))
        except ValueError:
            continue
        ids.add(stored_global_tray(ams_id, tray_id))
    return ids


def _global_tray_id_to_ams_slot(tray: int) -> tuple[int, int]:
    """Convert global tray id to (ams_id, tray_id) tuple for fallback tag generation.

    ``utils/tray_ids.slot_key``: an external input under ams 255 (254 -> 0,
    255 -> 1, a direct rack's 247-253 -> 2-8), an AMS-HT its own id with one
    tray, a rack AMS (28-36 / 37-45) its unit 32 / 33, a standard AMS four
    trays each.
    """
    return tray_slot_key(tray)


def _get_fallback_spool_tag(printer_serial: str, global_tray_id: int) -> str:
    """Mirror frontend getFallbackSpoolTag(serial, amsId, trayId) exactly."""
    if not printer_serial:
        return ""
    ams_id, tray_id = _global_tray_id_to_ams_slot(global_tray_id)
    return get_fallback_spool_tag_for_slot(printer_serial, ams_id, tray_id)


def get_fallback_spool_tag_for_slot(printer_serial: str, ams_id: int, tray_id: int) -> str:
    """Public helper matching frontend getFallbackSpoolTag(serial, amsId, trayId).

    Used by stale-tag cleanup (#1457) to detect Spoolman spools still holding
    this slot's deterministic fallback tag in extra.tag.
    """
    if not printer_serial:
        return ""
    return f"{_hash_serial_to_hex32(printer_serial)}{_to_fixed_hex(ams_id, 4)}{_to_fixed_hex(tray_id, 4)}"


def _resolve_spool_tag(tray_info: dict, printer_serial: str = "", global_tray_id: int | None = None) -> str:
    """Get the best spool identifier from tray info (prefer tray_uuid over tag_uid).

    Returns empty string if no usable identifier is found.
    """
    tray_uuid = str(tray_info.get("tray_uuid", "") or "")
    tag_uid = str(tray_info.get("tag_uid", "") or "")
    if tray_uuid and tray_uuid != _ZERO_UUID and _is_non_zero_identifier(tray_uuid):
        return tray_uuid
    if tag_uid and tag_uid != _ZERO_TAG_UID and _is_non_zero_identifier(tag_uid):
        return tag_uid
    if global_tray_id is not None:
        return _get_fallback_spool_tag(printer_serial, global_tray_id)
    return ""


EXTERNAL_HOLDERS = (254, 255)


def attribution_tray_tag(ams_trays: dict, global_tray_id: int, printer_serial: str) -> str:
    """The tag a print charge resolves a tray's spool by — the ONE rule (C047 S26, S34).

    Both the charge writer and the delete's guard ask this, so they cannot part:

    - a tray with a non-empty record in the start snapshot — its RFID, else its
      fallback tag (an empty record ``{}`` counts as no record, as the writer's
      ``if not tray_info`` always did);
    - the external holders 254 / 255 without a record — their fallback tag: the
      holder may be in the mapping without having been listed at print start;
    - any other tray without a record — no tag. An invalid id from an old mapping
      (256, which ``list[int]`` lets through) is not a holder and gets none.
    """
    tray_info = ams_trays.get(global_tray_id)
    if tray_info:
        return _resolve_spool_tag(tray_info, printer_serial, global_tray_id)
    if global_tray_id in EXTERNAL_HOLDERS:
        return _resolve_spool_tag({}, printer_serial, global_tray_id)
    return ""


def guard_tray_tags(ams_trays: dict, printer_serial: str) -> set[str]:
    """Every tag a running print's final charge could resolve a spool by (C047 S26).

    The tray a charge takes its weight from is known only at completion (stored
    mapping, tray log, live state), so the guard takes the superset: every tray
    of the snapshot with a record, and the two external holders always.
    """
    trays = {tray for tray, info in ams_trays.items() if isinstance(tray, int) and info} | set(EXTERNAL_HOLDERS)
    return {tag for tray in trays if (tag := attribution_tray_tag(ams_trays, tray, printer_serial))}


async def _get_printer_serial(printer_id: int) -> str:
    """Get printer serial for deterministic fallback tag generation."""
    from backend.app.models.printer import Printer
    from backend.app.services.printer_manager import printer_manager

    printer_info = printer_manager.get_printer(printer_id)
    if printer_info and printer_info.serial_number:
        return printer_info.serial_number

    async with async_session() as db:
        result = await db.execute(select(Printer.serial_number).where(Printer.id == printer_id))
        serial_number = result.scalar_one_or_none()
        return serial_number or ""


def _resolve_global_tray_id(slot_id: int, slot_to_tray: list | None, ams_trays: dict | None = None) -> int:
    """Map a 1-based slot_id to a global_tray_id using optional custom mapping.

    Custom mapping: ``slot_to_tray[slot_id - 1]`` is used when ``>= 0``.

    A value of ``-1`` in the custom mapping means the slicer routed this
    slot to the **external spool**. BambuStudio converts virtual tray IDs
    (254 / 255) to ``-1`` in the flat ``ams_mapping`` array before sending
    it to the printer — see ``start_print()`` in ``bambu_mqtt.py`` which
    documents this convention. We mirror it here: when ``-1`` is seen,
    look up the external spool's actual ``global_tray_id`` (254 / 255) in
    ``ams_trays`` rather than falling through to the position-based
    default (which would map ``slot_id=1`` to the first AMS tray and
    credit an unrelated spool — upstream Bambuddy #1276, regression of
    #853). With no external holder listed in ``ams_trays`` it is 254 all
    the same — the internal writer's answer — never a positional tray.

    Position-based default: uses sorted ``ams_trays`` keys so external
    spools (ID 254 / 255) naturally follow standard AMS trays, matching
    the slicer's slot numbering.

    Final fallback: ``slot_id - 1`` (legacy, works for pure AMS without
    external spools).
    """
    if slot_to_tray and slot_id <= len(slot_to_tray):
        mapped_tray = slot_to_tray[slot_id - 1]
        if mapped_tray == 0 and ams_trays and 0 not in ams_trays:
            # AMS-less machines: BS remaps the external to 0 with
            # use_ams=False, so 0 is the external in disguise unless the
            # machine actually has an AMS0-T0 (2026-08-24, four minis).
            for ext_id in (254, 255):
                if ext_id in ams_trays:
                    return ext_id
        if mapped_tray >= 0:
            return mapped_tray
        if mapped_tray == -1 and ams_trays:
            # ``-1`` means external spool. 254 = ``VIRTUAL_TRAY_DEPUTY_ID``
            # (main on single-nozzle, left / deputy on H2D dual-nozzle);
            # 255 = ``VIRTUAL_TRAY_MAIN_ID``. Prefer 254 when both exist
            # since that's what single-nozzle printers report via
            # ``tray_now`` (upstream Bambuddy #1276 / commit 6fe00adb).
            for ext_id in (254, 255):
                if ext_id in ams_trays:
                    return ext_id
            # The marker says "the external holder" and the printer listed none
            # at print start (empty, or never configured). It is still the
            # holder — never the AMS tray that happens to sit at this slot's
            # position, whose spool fed nothing (upstream fb69a665d). The
            # internal writer resolves the same slot to 254; the charge then
            # goes to a spool bound to the holder, or is reported not recorded.
            return 254
    # Position-based default: sort available tray IDs so external spools
    # (254 / 255) come after standard AMS trays, matching the slicer's
    # slot assignment order.
    if ams_trays:
        sorted_tray_ids = sorted(ams_trays.keys())
        if slot_id <= len(sorted_tray_ids):
            return sorted_tray_ids[slot_id - 1]
    return slot_id - 1


def unlose_external_markers(slot_to_tray: list | None, queue_mapping: list | None) -> list | None:
    """Restore concrete external ids the send-side remap destroyed.

    BambuStudio converts 254/255 to ``-1`` in the COMMAND's ams_mapping before
    sending. On a single-external machine the (254, 255) scan recovers it, but
    a dual-external machine (X2D / H2 family) makes ``-1`` ambiguous — and the
    queue item still carries the pre-remap mapping, so a concrete id there
    outranks guessing (measured live 2026-08-27, archive 775: ``[0, 255]``
    went out as ``[0, -1]`` and the scan would have charged the LEFT holder's
    spool for a right-holder print). Slots the queue cannot answer for keep
    their ``-1`` and take the scan as before.
    """
    if not slot_to_tray or not queue_mapping:
        return slot_to_tray
    merged = list(slot_to_tray)
    changed = False
    for i, mapped in enumerate(merged):
        if mapped == -1 and i < len(queue_mapping) and isinstance(queue_mapping[i], int) and queue_mapping[i] >= 254:
            merged[i] = queue_mapping[i]
            changed = True
    return merged if changed else slot_to_tray


def build_ams_tray_lookup(raw_data: dict) -> dict[int, dict]:
    """Build lookup of global_tray_id -> tray info from printer state.

    Returns: {0: {"tray_uuid": "...", "tag_uid": "...", "tray_type": "..."}, ...}
    """
    lookup = {}
    ams_data = raw_data.get("ams", [])
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        pair = unit_type(ams_unit)
        for tray in ams_unit.get("tray", []):
            tray_id = int(tray.get("id", 0))
            # AMS-HT units are their own id, a rack AMS by its type (utils/tray_ids)
            lookup[stored_global_tray(ams_id, tray_id, pair[1] if pair else None)] = {
                "tray_uuid": tray.get("tray_uuid", ""),
                "tag_uid": tray.get("tag_uid", ""),
                "tray_type": tray.get("tray_type", ""),
            }

    # External spool(s) (vt_tray is a list, global_tray_id from each entry's "id")
    for vt in raw_data.get("vt_tray") or []:
        if vt.get("tray_type"):
            tray_id = int(vt.get("id", 254))
            lookup[tray_id] = {
                "tray_uuid": vt.get("tray_uuid", ""),
                "tag_uid": vt.get("tag_uid", ""),
                "tray_type": vt.get("tray_type", ""),
            }

    return lookup


def _snapshot_tray_remain(raw_data: dict) -> dict[str, dict]:
    """Capture per-slot ``remain%`` + ``tray_uuid`` at print start so the
    completion path can compute a remain-delta when 3MF data doesn't cover
    the slot (or there's no 3MF at all — #1820).

    Returns ``{"<ams_id>-<tray_id>": {"remain": int, "tray_uuid": str}}``.
    Only slots whose ``remain`` is usable are included — see
    ``usable_remain_percent``, the gate ``usage_tracker.on_print_start`` shares.
    An unusable value means the AMS has told us nothing about that slot and a
    delta would be meaningless.

    ⚠️ **This function feeds BOTH ends of the delta** — it is called once at
    print start and again at completion — so the gate lands on both, which is
    what closes the only zero-reading route that reaches the external spool.
    The vt_tray branch below is that route: no other weight-accounting path
    reads ``vt_tray`` at all.
    """
    snapshot: dict[str, dict] = {}
    ams_raw = raw_data.get("ams", [])
    ams_data = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
    for ams_unit in ams_data:
        if not isinstance(ams_unit, dict):
            continue
        ams_id = int(ams_unit.get("id", 0))
        for tray in ams_unit.get("tray", []):
            if not isinstance(tray, dict):
                continue
            tray_id = int(tray.get("id", 0))
            remain = usable_remain_percent(tray.get("remain"))
            if remain is not None:
                snapshot[f"{ams_id}-{tray_id}"] = {
                    "remain": remain,
                    "tray_uuid": tray.get("tray_uuid", "") or "",
                }
    vt_tray_raw = raw_data.get("vt_tray") or []
    if isinstance(vt_tray_raw, dict):
        vt_tray_raw = [vt_tray_raw]
    for vt in vt_tray_raw:
        if not isinstance(vt, dict):
            continue
        vt_id = int(vt.get("id", 254))
        # 254 → (255, 0), 255 → (255, 1), 247-253 → (255, 2-8) — usage_tracker's encoding.
        vt_tray_id = external_slot_index(vt_id)
        remain = usable_remain_percent(vt.get("remain"))
        if remain is not None:
            snapshot[f"255-{vt_tray_id}"] = {
                "remain": remain,
                "tray_uuid": vt.get("tray_uuid", "") or "",
            }
    return snapshot


async def store_print_data(
    printer_id: int,
    archive_id: int,
    file_path: str,
    db,
    printer_manager,
    ams_mapping: list[int] | None = None,
    plate_id: int | None = None,
):
    """First-evidence writer (C047 S41): see ``_store_print_data``."""
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    async with spoolman_activity.first_evidence("spoolman_tracking"):
        return await _store_print_data(
            printer_id, archive_id, file_path, db, printer_manager, ams_mapping=ams_mapping, plate_id=plate_id
        )


async def _store_print_data(
    printer_id: int,
    archive_id: int,
    file_path: str,
    db,
    printer_manager,
    ams_mapping: list[int] | None = None,
    plate_id: int | None = None,
):
    """Store Spoolman tracking data at print start (persisted to database).

    Per-print tracking is the primary weight-update path for Spoolman, mirroring
    how the internal Filament Inventory works. The legacy AMS-remain%-based
    sync is no longer used as a weight writer (#1119) — the AMS doesn't report
    `tray_weight` for non-RFID spools, so that path silently dropped every
    non-Bambu spool. This routine therefore runs whenever Spoolman is enabled,
    regardless of the deprecated ``spoolman_disable_weight_sync`` flag (which
    is kept in the schema + settings UI for backwards compatibility but has
    no effect on the sync paths anymore).
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.print_file_analysis import get_print_file_analysis

    # Check if Spoolman is enabled
    spoolman_enabled = await get_setting(db, "spoolman_enabled")
    if not spoolman_enabled or spoolman_enabled.lower() != "true":
        return

    # Get current AMS tray state up front — needed both for the 3MF path's
    # ams_trays field and for the remain%-delta snapshot (#1820 fallback for
    # no-3MF "Untitled" prints, mirroring usage_tracker.on_print_start). We
    # capture the snapshot ALWAYS (even when a 3MF is present) so partial-3MF
    # coverage can also fall back per-slot at completion.
    state = printer_manager.get_status(printer_id)
    ams_trays: dict[int, dict] = {}
    tray_remain_start: dict[str, dict] = {}
    if state and state.raw_data:
        ams_trays = build_ams_tray_lookup(state.raw_data)
        tray_remain_start = _snapshot_tray_remain(state.raw_data)

    # Try to read per-slot filament estimates from the 3MF. Two paths can leave
    # ``filament_usage`` empty: (1) fallback archive (no .gcode.3mf was
    # downloadable from the printer — "Untitled" prints, see #1820; our A6/A7
    # fallback passes file_path=""), (2) 3MF present but slice_info missing
    # per-filament estimates. Both fall through to the remain%-delta path.
    filament_usage: list | None = None
    layer_usage_json: dict | None = None
    filament_properties: dict | None = None
    full_path = (
        app_settings.base_dir / file_path
    )  # SEC-PATH-OK: file_path is archive.file_path — a server-generated base_dir-relative archive path, used read-only
    # ``bool(file_path)`` guard is essential: the A6/A7 no-3MF fallback passes
    # file_path="" and ``base_dir / ""`` is base_dir itself — a directory that
    # ``.exists()`` returns True for. Without the guard the no-3MF case would
    # try to parse a directory as a 3MF.
    # A ready live/finishing context remains readable even after retention
    # removed the archive file.  ``get_print_file_analysis`` decides whether a
    # cold parse is possible; this guard only excludes the genuine no-path
    # fallback archive.
    threemf_available = bool(file_path)
    queue_item = None
    if threemf_available:
        # Resolve the printing queue item once (#1697) — reused for both the
        # plate-scoped 3MF extract and the ams_mapping fallback below.
        queue_result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.archive_id == archive_id)
            .where(PrintQueueItem.status == "printing")
        )
        candidates = list(queue_result.scalars().all())
        # Duplicate claims do not establish which row supplied options. The
        # archive/caller still owns its plate; keep accounting with no guess.
        queue_item = candidates[0] if len(candidates) == 1 else None
        # Caller-supplied plate_id wins (archive.plate_index — our authoritative
        # dispatched-plate record); fall back to the queue item's plate_id. When both
        # are None (external/single-plate prints) the whole-file sum is correct.
        effective_plate_id = (
            plate_id if plate_id is not None else (queue_item.plate_id if queue_item is not None else None)
        )
        # The live snapshot and queue lookup are complete. Do not reserve a
        # pooled connection while the child parses a large archive.
        await db.commit()
        analysis = await get_print_file_analysis(
            printer_manager,
            printer_id,
            archive_id,
            full_path,
            effective_plate_id,
        )
        if analysis is not None:
            # The same plate-scoped immutable analysis serves the live card,
            # internal inventory and this durable Spoolman start snapshot.
            filament_usage = analysis.filament_usage or None
            layer_usage = analysis.layer_usage
            if layer_usage:
                # Convert int keys to string for JSON serialization
                layer_usage_json = {str(k): v for k, v in layer_usage.items()}
                logger.debug("[SPOOLMAN] Parsed %s layers from G-code", len(layer_usage))
            filament_properties = analysis.filament_properties
    else:
        # No 3MF on disk — common for "Untitled" prints whose .gcode.3mf was
        # never on the printer's FTP (A6/A7 fallback). Logged at debug since the
        # fallback path below picks up the slack when remain% is available.
        logger.debug("[SPOOLMAN] 3MF file not available: %s", full_path)

    # If neither path has anything useful, there's nothing to track.
    if not filament_usage and not tray_remain_start:
        if threemf_available:
            logger.debug("[SPOOLMAN] No filament usage data in 3MF for archive %s", archive_id)
        return

    # Prefer the explicit mapping captured from the print command, then fall back
    # to any queue mapping stored for scheduled/reprint jobs. When the command
    # mapping carries the lossy -1 external marker, the queue copy un-loses it.
    queue_map = None
    if queue_item and queue_item.ams_mapping:
        try:
            queue_map = json.loads(queue_item.ams_mapping)
        except json.JSONDecodeError:
            pass  # Ignore malformed AMS mapping; fall back to default slot assignment
    slot_to_tray = ams_mapping if ams_mapping is not None else None
    if not slot_to_tray:
        slot_to_tray = queue_map
    else:
        slot_to_tray = unlose_external_markers(slot_to_tray, queue_map)

    # Delete any existing row for this printer/archive (shouldn't exist, but just in case)
    await db.execute(
        delete(ActivePrintSpoolman)
        .where(ActivePrintSpoolman.printer_id == printer_id)
        .where(ActivePrintSpoolman.archive_id == archive_id)
    )

    # Insert new tracking data. ``filament_usage`` is stored as ``[]`` for the
    # no-3MF case (we keep the column NOT NULL — [] behaves identically to None
    # in the report paths, which read ``filament_usage or []``); report_usage
    # then falls back to ``tray_remain_start`` for those slots.
    tracking = ActivePrintSpoolman(
        printer_id=printer_id,
        archive_id=archive_id,
        filament_usage=filament_usage or [],
        ams_trays=ams_trays,
        slot_to_tray=slot_to_tray,
        layer_usage=layer_usage_json,
        filament_properties=filament_properties,
        tray_remain_start=tray_remain_start or None,
    )
    db.add(tracking)
    await db.commit()

    logger.info(
        "[SPOOLMAN] Stored tracking data for print: printer=%s, archive=%s (3mf=%s, remain_snapshot=%d slot(s))",
        printer_id,
        archive_id,
        "yes" if filament_usage else "no",
        len(tray_remain_start),
    )
    logger.debug("[SPOOLMAN] Filament usage: %s", filament_usage)
    logger.debug("[SPOOLMAN] AMS trays: %s", list(ams_trays.keys()))
    if slot_to_tray:
        logger.debug("[SPOOLMAN] Custom slot mapping: %s", slot_to_tray)
    if layer_usage_json:
        logger.debug("[SPOOLMAN] Layer usage data available for partial tracking")


async def attach_recovered_3mf(
    printer_id: int,
    archive_id: int,
    file_path: str,
    db,
    printer_manager,
    *,
    plate_id: int | None = None,
    archive_identity: str | None = None,
) -> str:
    """First-evidence writer (C047 S41): see ``_attach_recovered_3mf``."""
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    async with spoolman_activity.first_evidence("spoolman_tracking"):
        return await _attach_recovered_3mf(
            printer_id,
            archive_id,
            file_path,
            db,
            printer_manager,
            plate_id=plate_id,
            archive_identity=archive_identity,
        )


async def _attach_recovered_3mf(
    printer_id: int,
    archive_id: int,
    file_path: str,
    db,
    printer_manager,
    *,
    plate_id: int | None = None,
    archive_identity: str | None = None,
) -> str:
    """Give the print's tracking the 3MF that arrived after print start (upstream 005b34af6).

    ``store_print_data`` ran once, with whatever the start download left: a row with no 3MF figures, or no
    row at all when no slot reported a remain%. Without this the completion charged the remain% change
    alone -- whole percents for an RFID reel, nothing for a reel without one.

    - A row with no 3MF figures gets ``filament_usage`` / ``layer_usage`` / ``filament_properties`` and
      nothing else: the tray lookup, the mapping and the remain snapshot are the start's. A row exists only
      until the print is charged, so this is safe at any moment, the completion's own last-chance download
      included.
    - A row that already has its figures is never rewritten.
    - A missing row is made only while the print still runs: its usage session row
      (``active_print_sessions``) is stamped with this very archive, and no completion of the printer has
      begun. After that the charge has already taken the print's row and a new one would be left behind.
      Its tray lookup is the printer's now, its mapping the session's dispatched one, and it carries no
      remain snapshot -- a mid-print reading is not where the print began.

    Returns ``"filled"``, ``"created"`` or ``"unchanged"``.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.models.active_print_session import ActivePrintSession
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import usage_tracker
    from backend.app.services.print_file_analysis import get_print_file_analysis

    if not file_path:
        return "unchanged"
    spoolman_enabled = await get_setting(db, "spoolman_enabled")
    if not spoolman_enabled or spoolman_enabled.lower() != "true":
        return "unchanged"

    async def _tracking_row():
        result = await db.execute(
            select(ActivePrintSpoolman)
            .where(ActivePrintSpoolman.printer_id == printer_id)
            .where(ActivePrintSpoolman.archive_id == archive_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def _running_session():
        """The print's own start session, while no completion has begun -- else None."""
        if archive_identity is None or usage_tracker.completion_is_running(printer_id):
            return None
        row = await db.get(ActivePrintSession, printer_id, populate_existing=True)
        return row if row is not None and row.archive_identity == archive_identity else None

    tracking = await _tracking_row()
    if tracking is not None and tracking.filament_usage:
        return "unchanged"
    if tracking is None and await _running_session() is None:
        return "unchanged"

    queue_result = await db.execute(
        select(PrintQueueItem).where(PrintQueueItem.archive_id == archive_id).where(PrintQueueItem.status == "printing")
    )
    candidates = list(queue_result.scalars().all())
    queue_item = candidates[0] if len(candidates) == 1 else None
    effective_plate_id = plate_id if plate_id is not None else (queue_item.plate_id if queue_item is not None else None)
    queue_map = None
    if queue_item and queue_item.ams_mapping:
        try:
            queue_map = json.loads(queue_item.ams_mapping)
        except json.JSONDecodeError:
            queue_map = None

    # Nothing is held while the child parses the archive (as ``store_print_data``).
    await db.commit()
    analysis = await get_print_file_analysis(
        printer_manager,
        printer_id,
        archive_id,
        app_settings.base_dir / file_path,  # SEC-PATH-OK: archive.file_path, server-generated, read-only
        effective_plate_id,
    )
    filament_usage = (analysis.filament_usage or None) if analysis is not None else None
    if not filament_usage:
        return "unchanged"
    layer_usage_json = {str(k): v for k, v in analysis.layer_usage.items()} if analysis.layer_usage else None

    # Decided again after the wait: the completion may have taken the row, or the start may have written it.
    tracking = await _tracking_row()
    if tracking is not None:
        if tracking.filament_usage:
            return "unchanged"
        tracking.filament_usage = filament_usage
        tracking.layer_usage = layer_usage_json
        tracking.filament_properties = analysis.filament_properties
        await db.commit()
        logger.info("[SPOOLMAN] Late 3MF: archive %s tracking now carries the file's figures", archive_id)
        return "filled"

    session_row = await _running_session()
    if session_row is None:
        return "unchanged"
    state = printer_manager.get_status(printer_id)
    ams_trays = build_ams_tray_lookup(state.raw_data) if state and state.raw_data else {}
    slot_to_tray = session_row.ams_mapping or None
    slot_to_tray = unlose_external_markers(slot_to_tray, queue_map) if slot_to_tray else queue_map
    made = ActivePrintSpoolman(
        printer_id=printer_id,
        archive_id=archive_id,
        filament_usage=filament_usage,
        ams_trays=ams_trays,
        slot_to_tray=slot_to_tray,
        layer_usage=layer_usage_json,
        filament_properties=analysis.filament_properties,
        tray_remain_start=None,
    )
    db.add(made)
    await db.commit()
    made_id = made.id
    # The print may have ended while that commit was on its way: its report has
    # then looked for the row already, or works from its own copy. Either way
    # nothing would remove this one — it goes, by its own id only.
    if await _running_session() is None:
        await db.execute(delete(ActivePrintSpoolman).where(ActivePrintSpoolman.id == made_id))
        await db.commit()
        logger.info(
            "[SPOOLMAN] Late 3MF: archive %s finished meanwhile; its late tracking row is withdrawn", archive_id
        )
        return "unchanged"
    logger.info("[SPOOLMAN] Late 3MF: made the tracking row of archive %s (print still running)", archive_id)
    return "created"


async def cleanup_tracking(
    printer_id: int,
    archive_id: int,
    db,
    last_layer_num: int | None = None,
    last_progress: int | None = None,
    completion: CompletionSnapshot | None = None,
    status: str = "failed",
):
    """Report partial usage and clean up Spoolman tracking data for failed/aborted prints.

    ``completion`` is the completion event's own figures; without it only the captured
    ``last_layer_num`` / ``last_progress`` are known (see ``_report_partial_usage``).
    ``status`` is the print's outcome, for its usage-history rows.

    ONE charge ownership covers the partial report, the runout correction after it
    and the tracking delete (C047 S36, S40): one snapshot, one client, one token. A
    closed charge gate (restore, shutdown) writes nothing."""
    from backend.app.services import spoolman_charge  # noqa: PLC0415

    async with spoolman_charge.owning(f"cleanup_tracking archive={archive_id}", db) as charge:
        if charge is None:
            return
        await _cleanup_tracking(
            printer_id, archive_id, db, last_layer_num, last_progress, completion, status, charge=charge
        )


async def _cleanup_tracking(
    printer_id: int,
    archive_id: int,
    db,
    last_layer_num: int | None,
    last_progress: int | None,
    completion: CompletionSnapshot | None,
    status: str,
    *,
    charge,
):
    from backend.app.models.active_print_spoolman import ActivePrintSpoolman

    # Get tracking data first (needed for partial usage reporting)
    result = await db.execute(
        select(ActivePrintSpoolman)
        .where(ActivePrintSpoolman.printer_id == printer_id)
        .where(ActivePrintSpoolman.archive_id == archive_id)
    )
    tracking = result.scalar_one_or_none()

    if not tracking:
        logger.debug("[SPOOLMAN] No tracking data to clean up for printer=%s, archive=%s", printer_id, archive_id)
        return

    # ONE check of the binding numbers for the whole tail (m266, acceptance A01): the
    # partial report and the zero correction are judged by it, and it settles only after
    # both — a journal number is confirmed through the slot rows that name it, so they
    # must still be there when the zero correction asks.
    identity = None
    if charge.enabled:
        tail_client = await _get_spoolman_client_with_fallback(charge)
        if tail_client:
            identity = ChargeCheck(tail_client, printer_id, async_session)

    # Try to report partial usage before cleanup
    try:
        await _report_partial_usage(
            printer_id,
            tracking,
            last_layer_num=last_layer_num,
            last_progress=last_progress,
            completion=completion,
            status=status,
            charge=charge,
            identity=identity,
        )
    except Exception as e:
        logger.warning("[SPOOLMAN] Partial usage report failed: %s", e)

    # A runout that aborted the print still emptied the spool — the zero
    # correction applies to failures exactly as to completions.
    try:
        # The same ownership, snapshot and client as the partial report above (C047 S36).
        if charge.enabled:
            client = await _get_spoolman_client_with_fallback(charge)
            if client:
                events = await _load_journal_events(printer_id, archive_id)
                await _apply_runout_zero_corrections_spoolman(client, events, archive_id, identity=identity)
    except Exception as e:
        logger.warning("[SPOOLMAN] Runout zero corrections on cleanup failed: %s", e)

    # Delete tracking data
    await db.execute(
        delete(ActivePrintSpoolman)
        .where(ActivePrintSpoolman.printer_id == printer_id)
        .where(ActivePrintSpoolman.archive_id == archive_id)
    )
    await db.commit()
    logger.debug("[SPOOLMAN] Cleaned up tracking data for printer=%s, archive=%s", printer_id, archive_id)

    # Bindings the tail refuted: removed now that this print's rows are no longer its
    # evidence (their own sessions, after the commit above), and said once.
    if identity is not None:
        await identity.settle()
        if identity.changed:
            from backend.app.services import spool_assignment_notifications  # noqa: PLC0415

            async with async_session() as notify_db:
                await spool_assignment_notifications.notify_usage_spool_changed(
                    printer_id, identity.changed, notify_db, logger
                )


async def _load_journal_events(printer_id: int, archive_id: int) -> list:
    """The print's usage-journal rows (m153) — the shared attribution record."""
    from backend.app.services.print_usage_journal import load_events

    async with async_session() as db:
        return without_stale_refill_runouts(await load_events(db, printer_id, archive_id))


def _spool_cost_per_gram(spool: dict | None) -> float | None:
    """What one gram off this Spoolman spool costs, or None (upstream 39835437, #2591).

    ``filament.price`` is the catalogue figure for a full spool; ``price`` on the
    spool overrides it for a purchase that cost something else — the order the
    Spoolman UI presents them in. The divisor is the SPOOL'S OWN net weight
    (``initial_weight``, else ``filament.weight`` — upstream 2f7f17a89, #3194),
    the same figure the remain-delta path turns a percentage into grams with, so
    a spool that can be charged by percentage can always be priced, and a 250 g
    refill under a 1000 g catalogue filament is not priced at a quarter of its
    rate. The rule lives in ``spoolman_weights``, shared with what is shown.

    A missing or non-positive price is an UNPRICED spool, not a free one: None,
    so the caller falls back to the farm rate. A spool-level 0 falls through to
    the catalogue — importers write 0 for "not set" often enough.
    """
    return spool_price_per_gram(spool)


_ObservedCharge = spoolman_usage_history.Charge


@dataclass
class _PrintCost:
    """What a print's charges to Spoolman spools amounted to — weight and money.

    Recorded as each charge LANDS, so the archive describes the same grams
    Spoolman actually deducted. ``charged_grams`` is every gram charged to a
    spool, priced or not (the archive's weight — audit D6's carry-over: the
    Spoolman writer never told the archive what it charged); ``cost`` /
    ``priced_grams`` only the grams whose spool has a price (upstream
    39835437, #2591). The rest is covered at the farm rate by the caller.
    """

    charged_grams: float = 0.0
    cost: float = 0.0
    priced_grams: float = 0.0
    priced: int = 0
    unpriced: int = 0
    # Which print this is, and each charge as it landed — what the usage
    # history records (upstream 1d86cff5a, #3407). Observed only: nothing
    # above reads these.
    printer_id: int | None = None
    archive_id: int | None = None
    history_status: str = "completed"
    observed: list[_ObservedCharge] = field(default_factory=list)
    # The token of the Spoolman the charges land in — from the charge's admission (C047 S33).
    source_token: str | None = None

    def add(self, grams: float, spool: dict | None, label: str, *, spool_id: int | None = None) -> None:
        """Record ``grams`` charged to ``spool``. Call only after the charge succeeded.

        ``spool_id`` names the spool for the usage history — passed apart from
        ``spool``, which is None when its row could not be fetched for the
        price: a charge without a price is history all the same.
        """
        if grams <= 0:
            return
        self.charged_grams += grams
        rate = _spool_cost_per_gram(spool)
        if spool_id is not None:
            self.observed.append(_ObservedCharge(spool_id=spool_id, grams=grams, rate=rate, spool=spool))
        if rate is None:
            self.unpriced += 1
            logger.debug("[SPOOLMAN] %s: spool has no usable price, the farm rate will cover it", label)
            return
        self.cost += grams * rate
        self.priced_grams += grams
        self.priced += 1


async def _spool_for_price(client, spool_id: int, label: str) -> dict | None:
    """The spool row for its price, best-effort: a failed fetch leaves the grams unpriced."""
    try:
        return await client.get_spool(spool_id)
    except Exception as exc:  # noqa: BLE001 — the price is the least important thing here
        logger.debug("[SPOOLMAN] %s: could not fetch spool %s for its price: %s", label, spool_id, exc)
        return None


async def _report_journal_splits(
    client,
    filament_usage: list[dict],
    slot_to_tray: list | None,
    ams_trays: dict[int, dict],
    journal_events: list,
    layer_usage_raw: dict | None,
    bound: PrintedBound,
    method_label: str,
    runout_purge_grams: float = 0.0,
    cost_out: _PrintCost | None = None,
    uncharged_out: list[tuple[int, float]] | None = None,
    identity=None,
) -> tuple[int, set[int], list[tuple[int, float]]]:
    """Charge slots whose tray has journal (runout) boundaries per-segment.

    ``identity`` (a ``spoolman_spool_identity.ChargeCheck``) confirms each frozen number
    through the slot rows that name it now before it is charged (m266).

    Mirrors usage_tracker's journal split — same boundary reader, same layer
    math (``tray_split.split_weight`` up to ``bound``) — with ``use_spool`` on
    the spoolman ids frozen at event time. ``used_g`` is the weight already
    determined for this completion (partial for a print that stopped). Returns
    ``(spools_updated, handled_global_tray_ids, leftover_usage_items)``;
    leftovers are the slots with no boundaries, for the normal single-charge
    path.
    """
    from backend.app.services.usage_tracker import journal_boundaries_for_tray

    layer_usage = None
    if layer_usage_raw:
        try:
            layer_usage = {
                int(layer): {int(fid): mm for fid, mm in filaments.items()}
                for layer, filaments in layer_usage_raw.items()
            }
        except (TypeError, ValueError, AttributeError):
            layer_usage = None

    updated = 0
    handled: set[int] = set()
    leftovers: list[tuple[int, float]] = []
    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        used_g = usage.get("used_g", 0)
        global_tray_id = _resolve_global_tray_id(slot_id, slot_to_tray, ams_trays)
        boundaries = journal_boundaries_for_tray(journal_events, global_tray_id) if journal_events else []
        # A single boundary is an answer, not "no split" — the whole print on the
        # spool that fed it while the mapped slot was empty from the start. Both
        # inventory backends must read the helper the same way or attribution
        # diverges between them, which is the whole reason the helper is shared.
        if used_g <= 0 or not boundaries:
            leftovers.append((slot_id, used_g))
            continue

        handled.add(global_tray_id)
        split = split_weight([start for start, _, _ in boundaries], float(used_g), slot_id, layer_usage, bound)
        if split is None:
            if sole_owner([(start, spoolman) for start, _, spoolman in boundaries]) is None:
                logger.info(
                    "[SPOOLMAN] %s: tray %d — no printed boundary (%s) to divide %.2fg between its reels, not recorded",
                    method_label,
                    global_tray_id,
                    bound.source,
                    float(used_g),
                )
                if uncharged_out is not None:
                    uncharged_out.append((global_tray_id, round(float(used_g), 2)))
                seg_grams = [0.0] * len(boundaries)
            else:  # one reel fed every stretch: the whole weight is its, divided or not
                seg_grams = [float(used_g)] + [0.0] * (len(boundaries) - 1)
        else:
            seg_grams = list(split.grams)
            if split.unknown_start > 0 and uncharged_out is not None:
                uncharged_out.append((global_tray_id, round(split.unknown_start, 2)))
        from backend.app.services.usage_tracker import _autoswitch_purge_for_tray

        _purge = _autoswitch_purge_for_tray(journal_events or [], global_tray_id, runout_purge_grams)
        if _purge > 0 and len(seg_grams) > 1:
            seg_grams[1] += _purge
        for (seg_start, _spool_id, spoolman_id), grams in zip(boundaries, seg_grams, strict=True):
            if grams <= 0:
                continue
            if not spoolman_id:
                logger.info(
                    "[SPOOLMAN] %s: journal segment from layer %d tray %d has no spool — %.2fg uncharged "
                    "rather than guessed",
                    method_label,
                    seg_start,
                    global_tray_id,
                    grams,
                )
                if uncharged_out is not None:
                    uncharged_out.append((global_tray_id, round(float(grams), 2)))
                continue
            admitted = None
            if identity is not None:
                # The frozen number must still name the spool the binding meant (m266).
                admission = await identity.admits(spoolman_id, tray=global_tray_id, grams=grams)
                if not admission.ok:
                    continue
                admitted = admission.spool
            try:
                await client.use_spool(spoolman_id, round(grams, 2))
                updated += 1
                # Priced only after the charge landed; the frozen id resolved
                # nothing else, so the row is fetched for its price.
                if cost_out is not None:
                    cost_out.add(
                        round(grams, 2),
                        admitted or await _spool_for_price(client, spoolman_id, method_label),
                        f"{method_label} journal slot {slot_id}",
                        spool_id=spoolman_id,
                    )
                logger.info(
                    "[SPOOLMAN] %s: slot %s journal split from layer %d: %.2fg -> spool %s",
                    method_label,
                    slot_id,
                    seg_start,
                    grams,
                    spoolman_id,
                )
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
                logger.warning("[SPOOLMAN] Journal split: use_spool(%s) failed: %s", spoolman_id, exc)
    return updated, handled, leftovers


async def _apply_runout_zero_corrections_spoolman(client, journal_events: list, archive_id: int, identity=None) -> int:
    """Close run-out Spoolman spools at exactly empty — after all print writes.

    ``identity`` (a ``spoolman_spool_identity.ChargeCheck``): a frozen number that names
    another spool now is neither drained, clamped nor archived (m266).

    Mirrors ``usage_tracker.apply_runout_zero_corrections``: only unambiguous
    runouts with a positively-known slot and a frozen spoolman id; positive
    remaining is drained via ``use_spool`` (Spoolman's own ledger keeps the
    step), negative remaining is clamped to 0 via ``update_spool`` (a downward
    move is a correction, not consumption). A pause-runout drains only when
    its episode was CLOSED by a spool_loaded — a reinserted same reel keeps
    printing and is not empty; an autoswitch is closed by definition. The
    negative clamp stays unconditional (impossible arithmetic either way).
    With ``runout_archive_spool_enabled`` on, a closed episode also archives
    the spool in Spoolman; a failed archive keeps the drain.
    """
    from backend.app.models.print_usage_event import (
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        KIND_AMBIGUOUS,
        KIND_AUTOSWITCH,
        KIND_MANUAL,
    )

    journal_events = without_stale_refill_runouts(journal_events)
    runouts = [
        e
        for e in (journal_events or [])
        if getattr(e, "event", "") == EVENT_RUNOUT
        and getattr(e, "kind", None) not in (KIND_AMBIGUOUS, KIND_MANUAL)
        and getattr(e, "global_tray_id", None) is not None
        and getattr(e, "spoolman_spool_id", None)
    ]
    if not runouts:
        return 0

    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting

        raw = await get_setting(db, "runout_zero_point_enabled")
        archive_raw = await get_setting(db, "runout_archive_spool_enabled")
    if raw is not None and raw.lower() == "false":
        return 0
    # Off unless the row says so — mirrors usage_tracker._archive_on_runout_enabled.
    archive_on_close = archive_raw is not None and archive_raw.lower() == "true"

    updated = 0
    archived_any = False
    for event in runouts:
        spool_id = event.spoolman_spool_id
        loads_after = [
            e
            for e in (journal_events or [])
            if getattr(e, "event", "") == EVENT_SPOOL_LOADED
            and getattr(e, "global_tray_id", None) == event.global_tray_id
            and getattr(e, "id", 0) > getattr(event, "id", 0)
        ]
        episode_closed = getattr(event, "kind", None) == KIND_AUTOSWITCH or bool(loads_after)
        # Retiring needs a DIFFERENT reel (or none assigned yet) — a load that
        # froze this same spool is the reel re-fed; mirrors usage_tracker.
        reel_replaced = getattr(event, "kind", None) == KIND_AUTOSWITCH or any(
            getattr(e, "spoolman_spool_id", None) != spool_id for e in loads_after
        )
        try:
            spool = await client.get_spool(spool_id)
        except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
            logger.debug("[SPOOLMAN] Zero correction: get_spool(%s) failed: %s", spool_id, exc)
            if identity is not None and isinstance(exc, SpoolmanNotFoundError):
                await identity.judge(spool_id, None)
            continue
        if identity is not None and not await identity.judge(spool_id, spool):
            continue
        remaining = spool.get("remaining_weight")
        if remaining is not None:
            try:
                if remaining > 0 and not episode_closed:
                    logger.info(
                        "[SPOOLMAN] Archive %s: runout on spool %s stays OPEN (no replacement seen) — not draining",
                        archive_id,
                        spool_id,
                    )
                elif remaining > 0:
                    await client.use_spool(spool_id, round(float(remaining), 2))
                    updated += 1
                    logger.info(
                        "[SPOOLMAN] Archive %s: runout zero-point drained %.2fg from spool %s",
                        archive_id,
                        remaining,
                        spool_id,
                    )
                elif remaining < 0:
                    await client.update_spool(spool_id, remaining_weight=0)
                    updated += 1
                    logger.info(
                        "[SPOOLMAN] Archive %s: runout zero-point clamped spool %s (%.2fg over) to 0",
                        archive_id,
                        spool_id,
                        -remaining,
                    )
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
                logger.warning("[SPOOLMAN] Zero correction failed for spool %s: %s", spool_id, exc)
        # The reel is demonstrably empty only on a CLOSED episode. The archive
        # does not depend on which way the drain went, nor on Spoolman knowing
        # a remaining figure at all; and a failed archive never undoes a drain
        # that already happened — two independent writes.
        if archive_on_close and reel_replaced and not spool.get("archived"):
            try:
                await client.set_spool_archived(spool_id, archived=True)
                archived_any = True
                logger.info("[SPOOLMAN] Archive %s: runout closed spool %s — archived", archive_id, spool_id)
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
                logger.warning("[SPOOLMAN] Archiving run-out spool %s failed: %s", spool_id, exc)
    if archived_any:
        from backend.app.core.websocket import ws_manager

        await ws_manager.broadcast({"type": "inventory_changed"})
    return updated


async def _get_spoolman_client_with_fallback(charge=None):
    """The client a print's accounting tail charges with: its charge's OWN client (C047 S33, S36).

    Built on the address of the charge's snapshot and health-checked once; never
    the global client, which ``/disconnect`` and ``/connect`` close under it even
    when the settings did not change. None when there is no reachable Spoolman.
    """
    if charge is None:
        return None
    return await charge.client()


async def _resolve_spool_id_via_slot_assignment(printer_id: int, ams_id: int, tray_id: int) -> int | None:
    """Look up the Spoolman spool ID locally bound to (printer, ams, tray).

    Fallback path for #1459: when a tag-less spool was assigned via the
    BamDude UI, the user's deterministic fallback tag is intentionally NOT
    written to Spoolman's extra.tag (kept clean per #1457), so
    find_spool_by_tag misses. The local spoolman_slot_assignments table is
    the authoritative binding for those spools.
    """
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    async with async_session() as db:
        result = await db.execute(
            select(SpoolmanSlotAssignment.spoolman_spool_id).where(
                SpoolmanSlotAssignment.printer_id == printer_id,
                SpoolmanSlotAssignment.ams_id == ams_id,
                SpoolmanSlotAssignment.tray_id == tray_id,
            )
        )
        return result.scalar_one_or_none()


async def _resolve_bound_spool(printer_id: int, ams_id: int, tray_id: int) -> tuple[int, str | None] | None:
    """The Spoolman spool bound to (printer, ams, tray): its number and the row's stamp (m266).

    What a charge reads from the slot ledger: the stamp says which spool the number named
    when it was bound (``spoolman_spool_identity.ChargeCheck``).
    """
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    async with async_session() as db:
        row = (
            await db.execute(
                select(SpoolmanSlotAssignment.spoolman_spool_id, SpoolmanSlotAssignment.spool_registered).where(
                    SpoolmanSlotAssignment.printer_id == printer_id,
                    SpoolmanSlotAssignment.ams_id == ams_id,
                    SpoolmanSlotAssignment.tray_id == tray_id,
                )
            )
        ).first()
    return (row.spoolman_spool_id, row.spool_registered) if row else None


def _usage_by_tray(
    usage_items: list[tuple[int, float]], slot_to_tray: list | None, ams_trays: dict[int, dict]
) -> list[tuple[int, float]]:
    """``(global tray id, grams)`` for every slot that used something — the shape of "not recorded"."""
    return [
        (_resolve_global_tray_id(slot_id, slot_to_tray, ams_trays), round(float(grams), 2))
        for slot_id, grams in usage_items
        if grams > 0
    ]


async def _say_usage_lost(printer_id: int, archive_id: int | None, lost: list[tuple[int, float]], db) -> None:
    """Spoolman did not answer at the end of a print: say which print lost what (upstream fb69a665d).

    The tracking row is already gone and nothing retries, so this is the only trace the
    usage leaves: a WARNING naming the printer, the archive, each tray — with the Spoolman
    spool a local slot assignment binds to it, the one spool identity knowable without
    Spoolman — and its grams; and the "usage not recorded" notice both backends already
    raise for filament that reached no spool. Never raises.
    """
    try:
        parts = []
        for tray, grams in lost:
            ams_id, tray_id = _global_tray_id_to_ams_slot(tray)
            try:
                spool_id = await _resolve_spool_id_via_slot_assignment(printer_id, ams_id, tray_id)
            except Exception:  # noqa: BLE001 — the id only decorates the log line
                spool_id = None
            parts.append(
                f"tray {tray} (spool {spool_id}): {grams:.2f} g"
                if spool_id is not None
                else f"tray {tray}: {grams:.2f} g"
            )
        logger.warning(
            "[SPOOLMAN] Not reachable at the end of the print (printer=%s archive=%s) — its usage is NOT recorded "
            "and is not retried: %s",
            printer_id,
            archive_id,
            ", ".join(parts) if parts else "no per-slot weight could be worked out without Spoolman",
        )
        if lost:
            from backend.app.services import spool_assignment_notifications

            await spool_assignment_notifications.notify_usage_not_recorded(printer_id, lost, db, logger)
    except Exception as exc:  # noqa: BLE001 — reporting a loss must not raise another
        logger.warning("[SPOOLMAN] Could not report the lost usage of archive %s: %s", archive_id, exc)


async def _report_spool_usage_for_slots(
    client,
    filament_usage_items: list[tuple[int, float]],
    ams_trays: dict[int, dict],
    slot_to_tray: list | None,
    method_label: str,
    printer_serial: str = "",
    printer_id: int | None = None,
    slot_colors_out: dict[int, str] | None = None,
    slot_materials_out: dict[int, str] | None = None,
    uncharged_out: list[tuple[int, float]] | None = None,
    cost_out: _PrintCost | None = None,
    identity=None,
) -> int:
    """Report usage to Spoolman for a list of (slot_id, grams) pairs.

    ``identity`` (a ``spoolman_spool_identity.ChargeCheck``) confirms a number the slot
    binding gave before it is charged (m266); the tag's spool is its own proof.

    Resolution order per slot: (1) Spoolman extra.tag match against the
    tray's RFID or deterministic fallback tag, (2) #1459 fallback —
    local spoolman_slot_assignments table keyed by (printer_id, ams_id,
    tray_id). Without (2), tag-less spools assigned via the BamDude UI
    never get their weight decremented because their extra.tag is empty
    on the Spoolman side.

    When ``slot_colors_out`` is provided it is populated with
    ``{slot_id: color_hex}`` for every resolved spool — used by
    :func:`report_usage` to stamp the archive's filament colour from the
    Spoolman spool rather than the slicer's 3MF value (#1494).
    ``slot_materials_out`` is the same contract for ``filament.material``, so a
    slot mapped to a differently-typed spool than it was sliced for records the
    material actually consumed (#2563).

    Returns number of spools successfully updated.
    """
    spools_updated = 0
    for slot_id, grams_used in filament_usage_items:
        if grams_used <= 0:
            continue

        global_tray_id = _resolve_global_tray_id(slot_id, slot_to_tray, ams_trays)
        tray_info = ams_trays.get(global_tray_id)
        if not tray_info and is_external_id(global_tray_id):
            # An external holder the printer did not list at print start (empty, or never
            # configured there): no tag to read, but a spool may be bound to the holder —
            # and if none is, the grams are reported, not dropped (audit D4).
            tray_info = {}
        elif not tray_info:
            logger.debug("[SPOOLMAN] Slot %s: no tray at global_tray_id %s", slot_id, global_tray_id)
            continue

        is_external = is_external_id(global_tray_id)
        tray_type = tray_info.get("tray_type", "")
        logger.debug(
            "[SPOOLMAN] Slot %s resolved to global_tray_id %s (tray_type=%s, external=%s)",
            slot_id,
            global_tray_id,
            tray_type or "unknown",
            is_external,
        )

        spool_id_to_use: int | None = None
        resolution_path = ""
        # color_hex of the resolved spool's filament, for the #1494 archive
        # colour rewrite. The tag path already has the full spool object;
        # the slot-assignment path only yields an id and is fetched below.
        spool_color_hex: str | None = None
        spool_material: str | None = None
        # The full spool row, kept so the price (#2591) comes out of the fetch
        # the colour and material already pay for.
        spool_obj: dict | None = None

        spool_tag = attribution_tray_tag(ams_trays, global_tray_id, printer_serial)
        if spool_tag:
            spool = await client.find_spool_by_tag(spool_tag)
            if spool:
                spool_id_to_use = spool["id"]
                resolution_path = "tag"
                spool_obj = spool
                spool_color_hex = (spool.get("filament") or {}).get("color_hex")
                spool_material = (spool.get("filament") or {}).get("material")

        bound_stamp: str | None = None
        if spool_id_to_use is None and printer_id is not None:
            ams_id, tray_id = _global_tray_id_to_ams_slot(global_tray_id)
            bound_spool = await _resolve_bound_spool(printer_id, ams_id, tray_id)
            if bound_spool is not None:
                spool_id_to_use, bound_stamp = bound_spool
                resolution_path = "slot-assignment"

        if spool_id_to_use is None:
            # WARNING with the grams, and reported to the caller (audit D4, #2812):
            # the print reads as a success while this filament reaches no spool.
            logger.warning(
                "[SPOOLMAN] Slot %s: no spool resolved (tag=%s, no slot-assignment) — %.1f g not charged",
                slot_id,
                spool_tag[:16] if spool_tag else "none",
                grams_used,
            )
            if uncharged_out is not None:
                uncharged_out.append((global_tray_id, round(float(grams_used), 2)))
            continue

        if resolution_path == "slot-assignment" and identity is not None:
            # The binding's number must still name the bound spool (m266).
            admission = await identity.admits(
                spool_id_to_use, slot_stamp=bound_stamp, tray=global_tray_id, grams=grams_used
            )
            if not admission.ok:
                continue
            if admission.spool is not None:
                spool_obj = admission.spool
                spool_color_hex = (spool_obj.get("filament") or {}).get("color_hex")
                spool_material = (spool_obj.get("filament") or {}).get("material")

        # Record the spool's filament colour + material for the archive rewrites
        # (#1494 colour, #2563 type). The slot-assignment path resolved only an id,
        # so fetch the spool ONCE for whichever value is still missing. Strictly
        # best-effort: a fetch failure must never abort the weight reporting for
        # the remaining slots, so the catch is broad.
        if slot_colors_out is not None or slot_materials_out is not None or cost_out is not None:
            need_color = slot_colors_out is not None and spool_color_hex is None
            need_material = slot_materials_out is not None and spool_material is None
            need_price = cost_out is not None and spool_obj is None
            if need_color or need_material or need_price:
                try:
                    spool_obj = await client.get_spool(spool_id_to_use)
                    _fil = spool_obj.get("filament") or {}
                    if need_color:
                        spool_color_hex = _fil.get("color_hex")
                    if need_material:
                        spool_material = _fil.get("material")
                except Exception as exc:  # noqa: BLE001 — colour/material/price are non-critical
                    logger.debug("[SPOOLMAN] Slot %s: could not fetch spool filament: %s", slot_id, exc)
            if slot_colors_out is not None and spool_color_hex:
                slot_colors_out[slot_id] = spool_color_hex
            if slot_materials_out is not None and isinstance(spool_material, str) and spool_material.strip():
                slot_materials_out[slot_id] = spool_material.strip()

        try:
            await client.use_spool(spool_id_to_use, grams_used)
            logger.info(
                "[SPOOLMAN] %s: slot %s: %sg -> spool %s (via %s)",
                method_label,
                slot_id,
                grams_used,
                spool_id_to_use,
                resolution_path,
            )
            spools_updated += 1
            # Recorded only after the charge landed: a spool Spoolman refused
            # contributes neither weight nor cost.
            if cost_out is not None:
                cost_out.add(grams_used, spool_obj, f"{method_label} slot {slot_id}", spool_id=spool_id_to_use)
        except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
            logger.warning("[SPOOLMAN] Failed to record usage for spool %s: %s", spool_id_to_use, exc)

    return spools_updated


async def _report_spool_usage_split_by_tray_changes(
    client,
    filament_usage: list[dict],
    tray_changes: list[tuple[int, int]],
    ams_trays: dict[int, dict],
    layer_usage: dict[int, dict[int, float]] | None,
    bound: PrintedBound,
    method_label: str,
    printer_serial: str,
    printer_id: int,
    slot_colors_out: dict[int, str] | None = None,
    slot_materials_out: dict[int, str] | None = None,
    journal_events: list | None = None,
    runout_purge_grams: float = 0.0,
    cost_out: _PrintCost | None = None,
    uncharged_out: list[tuple[int, float]] | None = None,
    identity=None,
) -> tuple[int, set[int]]:
    """Split each slot's grams across ``tray_changes`` and charge per-segment.

    ``identity`` (a ``spoolman_spool_identity.ChargeCheck``) confirms a number the
    journal froze or the slot binding gave before it is charged (m266).

    Mirrors ``usage_tracker`` Path 1's tray-switch branch so Spoolman and the
    internal Spool inventory attribute mid-print AMS-backup switches identically
    (#1793 — reporter's origin spool was over-charged the whole print because
    this path didn't exist). ``utils.usage_attribution.feed_intervals`` gives
    both backends the same intervals and frozen reels, and
    ``tray_split.split_weight`` the same grams up to ``bound``; this function
    wraps the per-interval spool resolution + ``use_spool`` sink for the
    Spoolman side.

    Returns ``(spools_updated, handled_global_tray_ids)`` — the caller passes
    ``handled_global_tray_ids`` into the remain-delta fallback so a tray
    attributed here is not double-charged there.
    """
    from backend.app.utils.usage_attribution import (
        UNKNOWN_FEED,
        add_autoswitch_purge,
        feed_intervals,
        not_recorded_tray,
    )

    spools_updated = 0
    handled_global_tray_ids: set[int] = set()
    # Shared with usage_tracker: each feed interval charged exactly once, to
    # the reel the journal froze for it — not to whatever tag the tray holds at
    # completion, which is the reel that arrived LAST.
    intervals = feed_intervals(tray_changes, journal_events or [])

    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        total_weight = usage.get("used_g", 0)
        if total_weight <= 0 or slot_id <= 0 or not intervals:
            continue

        # Every tray of the slot's stretches is this split's, whatever share it ends up with:
        # a stretch that printed nothing or went unrecorded is not the remain-delta's to charge.
        handled_global_tray_ids.update(iv.tray for iv in intervals if iv.tray != UNKNOWN_FEED)
        split = split_weight([iv.start_layer for iv in intervals], float(total_weight), slot_id, layer_usage, bound)
        if split is None:
            if sole_owner([(iv.start_layer, iv.spoolman_spool_id if iv.frozen else None) for iv in intervals]) is None:
                # Nothing the printer reported tells how to divide the grams between the reels,
                # and a share would be a guess — the slot is not recorded.
                logger.info(
                    "[SPOOLMAN] %s: slot %d — no printed boundary (%s) to divide %.2fg by, not recorded",
                    method_label,
                    slot_id,
                    bound.source,
                    float(total_weight),
                )
                if uncharged_out is not None:
                    uncharged_out.append(
                        (not_recorded_tray([iv.tray for iv in intervals]), round(float(total_weight), 2))
                    )
                interval_grams = [0.0] * len(intervals)
            else:  # one reel fed every stretch: the whole weight is its, divided or not
                interval_grams = [float(total_weight)] + [0.0] * (len(intervals) - 1)
        else:
            interval_grams = list(split.grams)
            if split.unknown_start > 0:
                # Printed before the first boundary: no stretch names its tray. The marker
                # tells the caller to close every tray to the remain-delta fallback.
                handled_global_tray_ids.add(UNKNOWN_FEED)
                if uncharged_out is not None:
                    uncharged_out.append((UNKNOWN_FEED, round(split.unknown_start, 2)))
        slot_intervals, interval_grams = add_autoswitch_purge(
            intervals, interval_grams, journal_events or [], runout_purge_grams
        )

        for seg_idx, (interval, segment_grams) in enumerate(zip(slot_intervals, interval_grams, strict=True)):
            if segment_grams <= 0:
                continue
            tray_global = interval.tray

            # Mark this tray as handled BEFORE the resolution attempt so
            # remain-delta doesn't double-charge it, even if we fail to find a
            # spool below. Matches usage_tracker behaviour: the tray was
            # physically fed from during this print, whether or not Spoolman
            # happens to have a matching row.
            handled_global_tray_ids.add(tray_global)

            tray_info = ams_trays.get(tray_global) or {}
            spool_id_to_use: int | None = None
            resolution_path = ""
            spool_color_hex: str | None = None
            spool_material: str | None = None
            spool_obj: dict | None = None
            # The slot row's stamp, for a number the binding gave (m266).
            seg_bound_stamp: str | None = None

            if interval.frozen:
                # The journal names this interval's reel; an unknown one stays
                # unattributed rather than charged to the reel there now.
                spool_id_to_use = interval.spoolman_spool_id
                resolution_path = "journal"
                if spool_id_to_use is None:
                    logger.info(
                        "[SPOOLMAN] Split slot %s seg %s tray=%d from layer %d: no attributable spool — "
                        "%.2fg uncharged rather than guessed",
                        slot_id,
                        seg_idx,
                        tray_global,
                        interval.start_layer,
                        segment_grams,
                    )
                    if uncharged_out is not None:
                        uncharged_out.append((tray_global, round(float(segment_grams), 2)))
                    continue
            else:
                spool_tag = _resolve_spool_tag(tray_info, printer_serial, tray_global) if tray_info else ""
                if spool_tag:
                    spool = await client.find_spool_by_tag(spool_tag)
                    if spool:
                        spool_id_to_use = spool["id"]
                        resolution_path = "tag"
                        spool_obj = spool
                        spool_color_hex = (spool.get("filament") or {}).get("color_hex")
                        spool_material = (spool.get("filament") or {}).get("material")

                if spool_id_to_use is None:
                    seg_ams_id, seg_tray_id = _global_tray_id_to_ams_slot(tray_global)
                    bound_spool = await _resolve_bound_spool(printer_id, seg_ams_id, seg_tray_id)
                    if bound_spool is not None:
                        spool_id_to_use, seg_bound_stamp = bound_spool
                        resolution_path = "slot-assignment"

            if spool_id_to_use is None:
                logger.info(
                    "[SPOOLMAN] Split slot %s seg %s tray=%d: no spool resolved — %.2fg lost from split accounting",
                    slot_id,
                    seg_idx,
                    tray_global,
                    segment_grams,
                )
                if uncharged_out is not None:
                    uncharged_out.append((tray_global, round(float(segment_grams), 2)))
                continue

            if identity is not None and resolution_path in ("journal", "slot-assignment"):
                # The number must still name the bound spool (m266); a tag is its own proof.
                admission = await identity.admits(
                    spool_id_to_use,
                    slot_stamp=seg_bound_stamp if resolution_path == "slot-assignment" else FROM_ROWS,
                    tray=tray_global,
                    grams=segment_grams,
                )
                if not admission.ok:
                    continue
                if admission.spool is not None:
                    spool_obj = admission.spool
                    spool_color_hex = (spool_obj.get("filament") or {}).get("color_hex")
                    spool_material = (spool_obj.get("filament") or {}).get("material")

            # Colour (#1494) + material (#2563) rewrite — first segment for a slot
            # wins. The UI displays a single colour/type per slot, so later segments
            # on the same slot don't overwrite (a backup swap can differ but the
            # archive card stays consistent with the origin).
            need_color = slot_colors_out is not None and slot_id not in slot_colors_out and spool_color_hex is None
            need_material = (
                slot_materials_out is not None and slot_id not in slot_materials_out and spool_material is None
            )
            # Unlike the colour, every segment needs its own price: each was
            # charged to its own spool, and a backup roll can have cost
            # something different from the one it replaced (#2591).
            need_price = cost_out is not None and spool_obj is None
            if need_color or need_material or need_price:
                try:
                    spool_obj = await client.get_spool(spool_id_to_use)
                    _fil = spool_obj.get("filament") or {}
                    if need_color:
                        spool_color_hex = _fil.get("color_hex")
                    if need_material:
                        spool_material = _fil.get("material")
                except Exception as exc:  # noqa: BLE001 — colour/material/price are non-critical
                    logger.debug("[SPOOLMAN] Split slot %s: could not fetch spool filament: %s", slot_id, exc)
            if slot_colors_out is not None and slot_id not in slot_colors_out and spool_color_hex:
                slot_colors_out[slot_id] = spool_color_hex
            if (
                slot_materials_out is not None
                and slot_id not in slot_materials_out
                and isinstance(spool_material, str)
                and spool_material.strip()
            ):
                slot_materials_out[slot_id] = spool_material.strip()

            try:
                await client.use_spool(spool_id_to_use, round(segment_grams, 2))
                logger.info(
                    "[SPOOLMAN] %s: slot %s seg %s tray=%d: %.2fg -> spool %s (via %s)",
                    method_label,
                    slot_id,
                    seg_idx,
                    tray_global,
                    segment_grams,
                    spool_id_to_use,
                    resolution_path,
                )
                spools_updated += 1
                if cost_out is not None:
                    cost_out.add(
                        round(segment_grams, 2),
                        spool_obj,
                        f"Split slot {slot_id} seg {seg_idx}",
                        spool_id=spool_id_to_use,
                    )
            except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
                logger.warning(
                    "[SPOOLMAN] Split slot %s seg %s: failed to record usage for spool %s: %s",
                    slot_id,
                    seg_idx,
                    spool_id_to_use,
                    exc,
                )

    return spools_updated, handled_global_tray_ids


async def _report_partial_usage(
    printer_id: int,
    tracking,
    last_layer_num: int | None = None,
    last_progress: int | None = None,
    completion: CompletionSnapshot | None = None,
    status: str = "failed",
    *,
    charge,
    identity=None,
):
    """Report what a print that stopped used: the slicer's per-slot estimate scaled by
    how far each slot got along its own G-code timeline (``partial_slot_grams`` — the
    internal writer's measure, through the same function). Falls back to linear
    interpolation if G-code data is unavailable.

    How far the print got comes from the completion event (``completion``, else the captured
    ``last_layer_num`` / ``last_progress``) — never from the printer now, which may already be
    running the next print. A layer estimated from the progress percentage sizes the weight
    only; it never divides it between reels (spec partial-print-layer-split r2).

    ``status`` is the print's own outcome, recorded on its usage-history rows only; the
    archive is settled as a failed print whatever it says.

    ``identity`` — the tail's ``ChargeCheck`` when the caller owns it (``_cleanup_tracking``:
    the zero correction after this report is judged by the same check, which the caller
    settles after both, acceptance A01). Without one this report makes and settles its own.
    """
    from backend.app.services.printer_manager import printer_manager
    from backend.app.utils.threemf_tools import partial_slot_grams

    owns_identity = identity is None

    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting

        # Check if partial usage reporting is enabled (default: true)
        report_partial = await get_setting(db, "spoolman_report_partial_usage")
        if report_partial and report_partial.lower() == "false":
            logger.debug("[SPOOLMAN] Partial usage reporting disabled by setting")
            return

    # Whether there is a Spoolman at all comes from the cleanup's charge snapshot (C047 S33, S36).
    if not charge.enabled:
        return

    # The printer's state is still read for what it alone holds — the tray chronology in the
    # client's memory and the remain% readings — never for the layer, layer count or progress.
    state = printer_manager.get_status(printer_id)
    snapshot = (
        completion
        if completion is not None
        else CompletionSnapshot.from_event({"last_layer_num": last_layer_num, "last_progress": last_progress})
    )
    bound = printed_bound(completed=False, snapshot=snapshot)
    total_layers = snapshot.total_layers
    if bound.source == "captured":
        logger.debug("[SPOOLMAN] Using captured last_layer_num=%s for partial usage", bound.layer)

    progress_percent = snapshot.progress_percent
    progress_ratio_from_event = min(progress_percent, 100.0) / 100.0 if progress_percent else None

    # The layer the partial weight is estimated at: the observed boundary, else one estimated
    # from the progress — for the weight alone.
    current_layer = bound.layer
    if not current_layer and progress_ratio_from_event and total_layers:
        current_layer = max(1, int(round(total_layers * progress_ratio_from_event)))
        logger.debug(
            "[SPOOLMAN] Estimated layer from progress=%s%% and total_layers=%s -> %s (weight only)",
            progress_percent,
            total_layers,
            current_layer,
        )

    # An unknown boundary is no unknown weight (PL-R1): with no layer at all, the progress still
    # sizes the weight by percentage below, and the split, having no boundary, reports it not
    # recorded unless one reel fed the whole print. Only with neither is there nothing to report.
    if not current_layer and progress_ratio_from_event is None:
        logger.debug(
            "[SPOOLMAN] No progress to report (layer 0/unknown, last_layer_num=%s, last_progress=%s)",
            last_layer_num,
            last_progress,
        )
        return

    logger.info("[SPOOLMAN] Reporting partial usage at layer %s/%s", current_layer or "?", total_layers or "?")

    # What the aborted print was actually charged, for the archive: its weight
    # is what was used, not the estimate, and the cost follows (#2591, D6).
    partial_archive_id = getattr(tracking, "archive_id", None)
    print_cost = _PrintCost(
        printer_id=printer_id,
        archive_id=partial_archive_id,
        history_status=status,
        source_token=charge.token,
    )
    # Filament read and not charged to any spool — from every branch below, said once.
    uncharged: list[tuple[int, float]] = []

    # What Spoolman, not answering, was never told — said once, with the print's name.
    lost: list[tuple[int, float]] = []

    async def _settle_archive() -> None:
        if client is None:
            async with async_session() as notify_db:
                await _say_usage_lost(printer_id, partial_archive_id, lost, notify_db)
            return
        async with async_session() as settle_db:
            await _settle_charges(settle_db, partial_archive_id, print_cost, status="failed")
        from backend.app.services import spool_assignment_notifications

        if uncharged:
            async with async_session() as notify_db:
                await spool_assignment_notifications.notify_usage_not_recorded(printer_id, uncharged, notify_db, logger)
        # Bindings the charges refuted (m266): removed now that this print's rows are no
        # longer its evidence, and said once — by whoever owns the check.
        if identity is not None and owns_identity:
            await identity.settle()
            if identity.changed:
                async with async_session() as notify_db:
                    await spool_assignment_notifications.notify_usage_spool_changed(
                        printer_id, identity.changed, notify_db, logger
                    )

    # Get tracking data
    layer_usage = tracking.layer_usage
    filament_usage = tracking.filament_usage or []
    ams_trays = {int(k): v for k, v in (tracking.ams_trays or {}).items()}
    slot_to_tray = tracking.slot_to_tray
    tray_remain_start = tracking.tray_remain_start or {}
    printer_serial = await _get_printer_serial(printer_id)

    # Not reachable: nothing can be charged, but what the print used is still worked out
    # below, so the loss can be named (``_say_usage_lost``) instead of dropped.
    client = await _get_spoolman_client_with_fallback(charge)
    # A number a binding or the journal gave is charged only to the bound spool (m266).
    if owns_identity:
        identity = ChargeCheck(client, printer_id, async_session) if client is not None else None

    # The print's journal: runout boundaries on any slot's tray, and — for a one-slot print —
    # the tray chronology's unknown feeds and identities. Read whatever the slot count.
    from backend.app.utils.usage_attribution import (
        chronology_splits,
        chronology_with_journal,
        normalize_tray_changes,
        sole_feeding_tray,
    )

    partial_journal: list = []
    try:
        partial_journal = await _load_journal_events(printer_id, getattr(tracking, "archive_id", -1))
    except Exception as exc:
        logger.debug("[SPOOLMAN] Journal load failed for partial report: %s", exc)

    # A one-slot print's stretches follow its tray chronology, as at completion and in the
    # internal writer: an AMS backup's takeover splits the grams, and a stretch nobody saw fed
    # is reported as not recorded — never charged to the mapped reel (post-rebuild F2-R1).
    partial_tray_changes: list[tuple[int, int]] = []
    partial_sole_tray: int | None = None
    if len([u for u in filament_usage if u.get("used_g", 0) > 0]) == 1:
        # The journal's unknown feed governs a memory that never heard of it (post-rebuild F5) —
        # and with no printer state at all, the journal is the chronology (F6).
        memory = list(getattr(state, "tray_change_log", []) or []) if state is not None else None
        partial_raw_log = chronology_with_journal(memory, partial_journal)
        partial_tray_changes = normalize_tray_changes(partial_raw_log)
        partial_sole_tray = sole_feeding_tray(partial_raw_log)

    # Which tray each slot came from — the completion writer's own ladder, so a print charges
    # the same spool whether it finished or stopped (upstream 991d2caca): the dispatched / queue
    # mapping from print start wins; without one, a one-slot print takes the tray the printer
    # named. A positional default is a guess: charged, and said at WARNING.
    slot_to_tray, mapping_source = _completion_slot_to_tray(
        slot_to_tray, filament_usage, partial_tray_changes, state, partial_sole_tray
    )
    if mapping_source in ("tray-log", "printer-tray"):
        logger.info(
            "[SPOOLMAN] Archive %s (stopped): charging the tray the printer named (%s): %s",
            partial_archive_id,
            mapping_source,
            slot_to_tray,
        )
    elif mapping_source == "positional" and any(u.get("used_g", 0) > 0 for u in filament_usage):
        logger.warning(
            "[SPOOLMAN] Archive %s (stopped): no mapping and no tray named by the printer — charging by tray "
            "position (right only for an AMS loaded in slicer order)",
            partial_archive_id,
        )

    async def _charge(usage_items: list[tuple[int, float]], label: str, split_layer_usage) -> int:
        if client is None:
            lost.extend(_usage_by_tray(usage_items, slot_to_tray, ams_trays))
            return 0
        if chronology_splits(partial_tray_changes):
            updated, _handled = await _report_spool_usage_split_by_tray_changes(
                client,
                [{"slot_id": slot_id, "used_g": grams} for slot_id, grams in usage_items],
                partial_tray_changes,
                ams_trays,
                split_layer_usage,
                bound,
                label,
                printer_serial,
                printer_id=printer_id,
                journal_events=partial_journal,
                cost_out=print_cost,
                uncharged_out=uncharged,
                identity=identity,
            )
            return updated
        # A reel changed in a slot's own tray splits that slot as at completion; only the slots
        # without such boundaries go to the mapping, once.
        updated, _handled, leftovers = await _report_journal_splits(
            client,
            [{"slot_id": slot_id, "used_g": grams} for slot_id, grams in usage_items],
            slot_to_tray,
            ams_trays,
            partial_journal,
            split_layer_usage,
            bound,
            label,
            cost_out=print_cost,
            uncharged_out=uncharged,
            identity=identity,
        )
        return updated + await _report_spool_usage_for_slots(
            client,
            leftovers,
            ams_trays,
            slot_to_tray,
            label,
            printer_serial,
            printer_id=printer_id,
            uncharged_out=uncharged,
            cost_out=print_cost,
            identity=identity,
        )

    # No-3MF aborted print (#1820 mirror of the completion path): no slicer
    # estimate in filament_usage to scale (a G-code timeline alone is a
    # progress measure, not a weight), but the remain%-delta snapshot we
    # captured at start still describes consumption up to the abort moment.
    # Write it the same way report_usage's fallback does, then return —
    # there's no 3MF-derived partial to layer on top.
    if not filament_usage and tray_remain_start:
        if client is None:
            await _settle_archive()
            return
        current_lookup = _snapshot_tray_remain(state.raw_data) if state and state.raw_data else {}
        # Journal trays are off-limits to the delta here too (a runout's
        # substitute tray must not be charged twice on an abort).
        journal_handled = {e.global_tray_id for e in partial_journal if getattr(e, "global_tray_id", None) is not None}
        await _report_remain_delta_for_slots(
            client,
            printer_id=printer_id,
            tray_remain_start=tray_remain_start,
            current_lookup=current_lookup,
            handled_global_tray_ids=journal_handled,
            archive_id=getattr(tracking, "archive_id", -1),
            cost_out=print_cost,
            identity=identity,
        )
        await _settle_archive()
        return

    # The linear ratio: what sizes a slot the G-code timeline says nothing about.
    progress_ratio = None
    if current_layer and total_layers and total_layers > 0:
        progress_ratio = min(current_layer / total_layers, 1.0)
    elif progress_ratio_from_event is not None:
        progress_ratio = progress_ratio_from_event

    # The slicer's estimate x each slot's progress along its own G-code timeline — the measure
    # the internal writer uses, through the same function (``partial_slot_grams``), so a stopped
    # print weighs the same in both backends. Never the G-code's own grams: flush / purge runs
    # in firmware macros and is not in the plate G-code (measured 46.35 g against 6.8 g of
    # E-moves), and the parser counts every un-retract as extrusion (upstream a8dd164c7, #3317).
    # It measures the weight AT a layer, so it needs an observed one — as the internal writer
    # does; a layer estimated from the progress is the linear ratio's below.
    if layer_usage and bound.layer:
        layer_usage_int = {
            int(layer): {int(fid): mm for fid, mm in filaments.items()} for layer, filaments in layer_usage.items()
        }
        slot_grams = partial_slot_grams(filament_usage, layer_usage_int, bound.layer)

        if slot_grams:
            logger.info("[SPOOLMAN] Using the G-code timeline for layer %s", bound.layer)

            usage_items = []
            for usage in filament_usage:
                slot_id = usage.get("slot_id", 0)
                total_used_g = usage.get("used_g", 0)
                if total_used_g <= 0:
                    continue
                if slot_id in slot_grams:
                    usage_items.append((slot_id, round(slot_grams[slot_id], 2)))
                elif progress_ratio is not None:
                    usage_items.append((slot_id, round(total_used_g * progress_ratio, 2)))

            spools_updated = await _charge(usage_items, "Partial (G-code)", layer_usage_int)
            if spools_updated > 0:
                logger.info("[SPOOLMAN] Reported partial usage to %s spool(s) using G-code data", spools_updated)
            await _settle_archive()
            return

    # Fallback: linear interpolation (if no G-code data available)
    if progress_ratio is None:
        logger.debug(
            "[SPOOLMAN] Cannot use linear fallback: total_layers=%s, last_progress=%s",
            total_layers,
            last_progress,
        )
        if client is None:
            await _settle_archive()
        return

    logger.info("[SPOOLMAN] Falling back to linear interpolation (%s)", progress_ratio)

    usage_items = []
    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        total_used_g = usage.get("used_g", 0)
        if total_used_g > 0:
            partial_used_g = round(total_used_g * progress_ratio, 2)
            usage_items.append((slot_id, partial_used_g))

    spools_updated = await _charge(usage_items, "Partial (linear)", None)
    if spools_updated > 0:
        logger.info("[SPOOLMAN] Reported partial usage to %s spool(s) using linear interpolation", spools_updated)
    await _settle_archive()


def _single_slot_tray_from_state(state, tray_changes: list) -> int | None:
    """The tray the printer's own reporting says a one-slot print came from, or None
    (upstream 935d4b5b, #2953).

    The ladder ``usage_tracker`` has used for the same question: a single logged
    switch, then the current ``tray_now``, then ``last_loaded_tray`` (an A1 parks
    ``tray_now`` at 255 the moment a print ends), then 255 when it is a real
    external spool on an H2. More than one logged switch is the tray split's
    (#1793), not a single tray. ``tray_now_at_start`` is deliberately not a rung:
    the Spoolman tracking row does not keep it, and where this matters it reads
    255 anyway — print start runs before the filament loads.
    """
    from backend.app.utils.usage_attribution import chronology_splits

    if chronology_splits(tray_changes):
        return None
    if tray_changes:
        tray = tray_changes[0][0]
        if isinstance(tray, int) and 0 <= tray <= 254:
            return tray
    if state is None:
        return None
    tray_now = getattr(state, "tray_now", 255)
    # 255 too on a direct rack whose snow said PrintTool 1's input (``names_loaded_source``).
    if names_loaded_source(tray_now, state):
        return tray_now
    last_loaded = getattr(state, "last_loaded_tray", -1)
    if isinstance(last_loaded, int) and 0 <= last_loaded <= 253:
        return last_loaded
    if tray_now == 255 and not state_is_direct_rack(state):
        vt_tray = (getattr(state, "raw_data", None) or {}).get("vt_tray") or []
        if any(isinstance(vt, dict) and int(vt.get("id", 0)) == 255 for vt in vt_tray):
            return 255
    return None


def _completion_slot_to_tray(
    slot_to_tray: list | None,
    filament_usage: list[dict],
    tray_changes: list,
    state,
    sole_tray: int | None = None,
) -> tuple[list | None, str]:
    """The mapping the completion writer charges by, and where it came from.

    A one-slot print whose tray log moved but collapses to one tray
    (``usage_attribution.sole_feeding_tray``) is charged to that tray
    (``tray-log``): the log is the tray split's evidence and outranks the
    mapping, as the split did before the log was normalized.

    ⚠️ Priority otherwise unchanged (CLAUDE.md, "Filament attribution believes the
    DISPATCHED mapping"): the dispatched / queue mapping captured at start wins
    (``dispatched``). Without one, a multi-switch log is charged per segment by
    the tray split (``split`` — each segment to the tray the printer announced,
    so NOT a guess). A print with exactly one slot carrying usage takes the tray
    the printer named (``printer-tray``). Anything else is the positional
    default (``positional``) — right for an AMS loaded in slicer order, but a
    guess, and the caller treats it as one.
    """
    used = [u for u in filament_usage if (u.get("used_g") or 0) > 0]
    if sole_tray is not None and len(used) == 1:
        slot_id = int(used[0].get("slot_id") or 1)
        mapping = [-2] * slot_id
        mapping[slot_id - 1] = sole_tray
        return mapping, "tray-log"
    if slot_to_tray:
        return slot_to_tray, "dispatched"
    from backend.app.utils.usage_attribution import chronology_splits

    if chronology_splits(tray_changes):
        return None, "split"
    if len(used) == 1:
        tray = _single_slot_tray_from_state(state, tray_changes)
        if tray is not None:
            slot_id = int(used[0].get("slot_id") or 1)
            mapping = [-2] * slot_id
            mapping[slot_id - 1] = tray
            return mapping, "printer-tray"
    return None, "positional"


async def report_usage(printer_id: int, archive_id: int, completion: CompletionSnapshot | None = None):
    """Report a completed print's usage to Spoolman — inside ONE charge ownership (C047 §C1).

    The ownership is admitted before the tail's first write (the tracking row's
    delete), holds the snapshot's own client and token, and ends after the last
    write. A closed charge gate (restore, shutdown) writes nothing.
    """
    from backend.app.services import spoolman_charge  # noqa: PLC0415

    async with spoolman_charge.owning(f"report_usage archive={archive_id}") as charge:
        if charge is None:
            return
        await _report_usage(printer_id, archive_id, completion, charge)


async def _report_usage(printer_id: int, archive_id: int, completion: CompletionSnapshot | None, charge):
    """Report filament usage to Spoolman after print completion.

    ``completion`` is the completion event's own figures — the plate's layer count, the
    terminal and captured layers — the boundary a split divides up to; without it, nothing
    is known about the layers and a split is measured by the slot's G-code alone.

    Two writers, mirroring the internal-inventory split in usage_tracker:

    1. **3MF path (primary)** — per-filament slice estimates captured at
       print start drive a precise per-slot ``use_spool`` call.
    2. **AMS remain%-delta (fallback)** — for slots the 3MF path didn't
       handle (including the no-3MF "Untitled" case from #1820): compute
       ``start_remain - current_remain``, multiply by the resolved
       Spoolman filament's reference weight, and write the delta. Mirrors
       ``usage_tracker.on_print_complete`` Path 2.
    """
    async with async_session() as db:
        from backend.app.api.routes.settings import get_setting
        from backend.app.models.active_print_spoolman import ActivePrintSpoolman

        # Get tracking data stored at print start
        result = await db.execute(
            select(ActivePrintSpoolman)
            .where(ActivePrintSpoolman.printer_id == printer_id)
            .where(ActivePrintSpoolman.archive_id == archive_id)
        )
        tracking = result.scalar_one_or_none()

        if not tracking:
            logger.info("[SPOOLMAN] No tracking data for print (printer=%s, archive=%s)", printer_id, archive_id)
            return

        filament_usage = tracking.filament_usage or []
        ams_trays = {int(k): v for k, v in (tracking.ams_trays or {}).items()}
        slot_to_tray = tracking.slot_to_tray
        tray_remain_start = tracking.tray_remain_start or {}
        # ``layer_usage`` was added later than the base tracking fields; use
        # ``getattr`` so tests that stub ``tracking`` as a lightweight
        # SimpleNamespace stay valid, and historic ORM rows loaded without the
        # column can't AttributeError on read.
        layer_usage_raw = getattr(tracking, "layer_usage", None) or {}
        printer_serial = await _get_printer_serial(printer_id)

        # Delete tracking row (we're done with it)
        await db.delete(tracking)
        await db.commit()

        if not filament_usage and not tray_remain_start:
            logger.debug("[SPOOLMAN] No usage data or remain-snapshot for archive %s", archive_id)
            return

        # Whether there is a Spoolman at all comes from the charge's own snapshot (C047 S33).
        if not charge.enabled:
            return

        client = await _get_spoolman_client_with_fallback(charge)

        # Consult the live printer state for the tray-change log written by
        # ``bambu_mqtt.py`` on every mid-print ``tray_now`` change. When there's
        # more than one entry, the print traversed >1 AMS tray and the split
        # path attributes each segment to the tray that was loaded at the time
        # — matches the internal Spool inventory writer in ``usage_tracker.py``.
        # Without this, an AMS-backup runout switch charges the whole slot to
        # the origin spool and pushes it past ``initial_weight`` (#1793).
        #
        # Split only for SINGLE-slot prints — same gate as ``usage_tracker``.
        # Multi-slot (multi-colour) prints naturally cycle trays for every
        # colour change, so splitting each slot's grams across ALL
        # tray_change_log entries would attribute slot 1's grams to the segments
        # where slot 2's tray was loaded and vice versa. Multi-slot prints fall
        # through to the existing single-tray path (which uses the stable
        # ``slot_to_tray`` mapping).
        nonzero_slots = [u for u in filament_usage if u.get("used_g", 0) > 0]
        tray_changes: list[tuple[int, int]] = []
        sole_tray: int | None = None
        _state = None

        # The print's usage journal (m153): runout boundaries + frozen spool
        # ids, shared with the internal tracker so both backends attribute
        # identically — read before the chronology, whose unknown feeds it
        # governs (post-rebuild F5).
        journal_events: list = []
        try:
            journal_events = await _load_journal_events(printer_id, archive_id)
        except Exception as exc:
            logger.warning("[SPOOLMAN] Journal load failed for archive %s: %s", archive_id, exc)

        if len(nonzero_slots) == 1:
            from backend.app.services.printer_manager import printer_manager as _pm

            _state = _pm.get_status(printer_id)
            # Normalized the same way as usage_tracker: a same-layer blip
            # (BMCU tray_now=254 on every load) fed nothing. The journal's
            # unknown feed governs the memory (F5); with no printer state at
            # all, the journal is the chronology (F6).
            from backend.app.utils.usage_attribution import (
                chronology_with_journal,
                normalize_tray_changes,
                sole_feeding_tray,
            )

            memory = list(getattr(_state, "tray_change_log", []) or []) if _state is not None else None
            raw_tray_log = chronology_with_journal(memory, journal_events)
            tray_changes = normalize_tray_changes(raw_tray_log)
            sole_tray = sole_feeding_tray(raw_tray_log)
        # The boundary every split divides up to, from the completion event: the plate's layer
        # count, else — P1S firmware resets it at print end — the terminal or captured layer.
        # The printer is not asked: by now it may be running the next print.
        bound = printed_bound(completed=True, snapshot=completion or CompletionSnapshot())

        # Which tray each slot came from. The dispatched / queue mapping from
        # print start wins; below it, for a one-slot print, the tray the printer
        # itself named (upstream 935d4b5b, #2953). A positional default is a
        # guess: charged, said at WARNING, and it does not restamp the archive.
        slot_to_tray, mapping_source = _completion_slot_to_tray(
            slot_to_tray, filament_usage, tray_changes, _state, sole_tray
        )
        mapping_is_guess = mapping_source == "positional" and bool(nonzero_slots)

        if not client:
            # The tracking row is gone and nothing retries: name the print and what it used
            # — by the trays the mapping just resolved — instead of dropping it silently.
            await _say_usage_lost(
                printer_id,
                archive_id,
                _usage_by_tray(
                    [(u.get("slot_id", 0), u.get("used_g", 0)) for u in filament_usage], slot_to_tray, ams_trays
                ),
                db,
            )
            return
        if mapping_source == "tray-log":
            logger.info(
                "[SPOOLMAN] Archive %s: tray change log collapses to one tray; charging it: %s",
                archive_id,
                slot_to_tray,
            )
        elif mapping_source == "printer-tray":
            logger.info(
                "[SPOOLMAN] Archive %s: no dispatched mapping; charging the tray the printer named: %s",
                archive_id,
                slot_to_tray,
            )
        elif mapping_is_guess:
            logger.warning(
                "[SPOOLMAN] Archive %s: no mapping and no tray named by the printer — charging by tray "
                "position (right only for an AMS loaded in slicer order); the archive's colour and "
                "material are left as sliced",
                archive_id,
            )

        slot_colors: dict[int, str] = {}
        slot_materials: dict[int, str] = {}
        # A number a binding or the journal gave is charged only to the bound spool (m266).
        identity = ChargeCheck(client, printer_id, async_session)
        # Every charge that lands is recorded here — its weight, and its money
        # when the spool has a price — for the archive to settle on (#2591).
        print_cost = _PrintCost(
            printer_id=printer_id, archive_id=archive_id, history_status="completed", source_token=charge.token
        )
        handled_global_tray_ids: set[int] = set()
        # Filament read and not charged to any Spoolman spool (audit D4).
        uncharged: list[tuple[int, float]] = []
        spools_updated = 0

        # Optional emergency-swap purge (grams per autoswitch runout, default 0).
        _purge_str = await get_setting(db, "runout_purge_grams")
        try:
            runout_purge_grams = float(_purge_str) if _purge_str else 0.0
        except ValueError:
            runout_purge_grams = 0.0

        # --- Path 1: 3MF per-slot estimates -----------------------------
        from backend.app.utils.usage_attribution import UNKNOWN_FEED, chronology_splits

        if filament_usage:
            if chronology_splits(tray_changes):
                # Tray-split path — attribute per-segment to the tray that was
                # loaded at that time.
                logger.info(
                    "[SPOOLMAN] Reporting per-filament usage for archive %s with tray-split "
                    "(tray_change_log=%s, printed to layer %s from %s)",
                    archive_id,
                    tray_changes,
                    bound.layer,
                    bound.source,
                )
                # ``tracking.layer_usage`` was serialized to JSON so int keys
                # come back as strings. Restore them for the split math.
                layer_usage = None
                if layer_usage_raw:
                    try:
                        layer_usage = {
                            int(layer): {int(fid): mm for fid, mm in filaments.items()}
                            for layer, filaments in layer_usage_raw.items()
                        }
                    except (TypeError, ValueError, AttributeError):
                        # AttributeError catches ``inner.items()`` when the
                        # inner value isn't dict-shaped (corrupt JSON row).
                        # Missing gcode falls through to the linear-ratio branch
                        # inside ``split_weight`` — still a correct split, just
                        # less precise.
                        layer_usage = None
                split_updated, split_handled = await _report_spool_usage_split_by_tray_changes(
                    client,
                    filament_usage,
                    tray_changes,
                    ams_trays,
                    layer_usage,
                    bound,
                    f"Archive {archive_id}",
                    printer_serial,
                    printer_id=printer_id,
                    slot_colors_out=slot_colors,
                    slot_materials_out=slot_materials,
                    journal_events=journal_events,
                    runout_purge_grams=runout_purge_grams,
                    cost_out=print_cost,
                    uncharged_out=uncharged,
                    identity=identity,
                )
                spools_updated += split_updated
                handled_global_tray_ids |= split_handled
                if any(tray == UNKNOWN_FEED for tray, _layer in tray_changes) or UNKNOWN_FEED in split_handled:
                    # Grams nobody saw fed — a stretch, a backup's purge, the print before the
                    # first boundary — are reported as not recorded: no remain-delta fallback
                    # charges them to a tray afterwards.
                    handled_global_tray_ids |= _every_global_tray(tray_remain_start, ams_trays)
            else:
                logger.info("[SPOOLMAN] Reporting per-filament usage for archive %s", archive_id)
                # Slots whose tray has journal (runout) boundaries are split
                # per-segment on the frozen spoolman ids; the rest go through
                # the normal single-charge path below.
                split_updated, split_handled, usage_items = await _report_journal_splits(
                    client,
                    filament_usage,
                    slot_to_tray,
                    ams_trays,
                    journal_events,
                    layer_usage_raw,
                    bound,
                    f"Archive {archive_id}",
                    runout_purge_grams=runout_purge_grams,
                    cost_out=print_cost,
                    uncharged_out=uncharged,
                    identity=identity,
                )
                spools_updated += split_updated
                handled_global_tray_ids |= split_handled
                spools_updated += await _report_spool_usage_for_slots(
                    client,
                    usage_items,
                    ams_trays,
                    slot_to_tray,
                    f"Archive {archive_id}",
                    printer_serial,
                    printer_id=printer_id,
                    slot_colors_out=slot_colors,
                    slot_materials_out=slot_materials,
                    uncharged_out=uncharged,
                    cost_out=print_cost,
                    identity=identity,
                )
                # Track which physical slots the 3MF path already covered so
                # Path 2 doesn't double-charge them.
                # A slot that used nothing was never charged, so it claims no
                # tray — the remain%-delta path stays free to cover it (#2953).
                for slot_id, used in usage_items:
                    if used > 0:
                        handled_global_tray_ids.add(_resolve_global_tray_id(slot_id, slot_to_tray, ams_trays))

        # Belt-and-braces against the multicolour runout double-count: every
        # tray the journal names is off-limits to the remain%-delta path.
        handled_global_tray_ids |= {
            e.global_tray_id for e in journal_events if getattr(e, "global_tray_id", None) is not None
        }

        # --- Path 2: AMS remain%-delta for slots 3MF didn't cover -------
        # Triggered for no-3MF "Untitled" prints (#1820) AND for partial
        # 3MF coverage (slots whose filament_id wasn't in slice_info).
        if tray_remain_start:
            from backend.app.services.printer_manager import printer_manager

            current = printer_manager.get_status(printer_id)
            current_lookup = _snapshot_tray_remain(current.raw_data) if current and current.raw_data else {}
            fallback_updates = await _report_remain_delta_for_slots(
                client,
                printer_id=printer_id,
                tray_remain_start=tray_remain_start,
                current_lookup=current_lookup,
                handled_global_tray_ids=handled_global_tray_ids,
                archive_id=archive_id,
                slot_colors_out=slot_colors,
                slot_materials_out=slot_materials,
                cost_out=print_cost,
                identity=identity,
            )
            spools_updated += fallback_updates

        # --- Runout zero corrections, AFTER every print write ------------
        try:
            spools_updated += await _apply_runout_zero_corrections_spoolman(
                client, journal_events, archive_id, identity=identity
            )
        except Exception as exc:
            logger.warning("[SPOOLMAN] Runout zero corrections failed for archive %s: %s", archive_id, exc)

        if spools_updated == 0:
            logger.info("[SPOOLMAN] Archive %s: no spools updated", archive_id)
        else:
            logger.info("[SPOOLMAN] Archive %s: updated %s spool(s)", archive_id, spools_updated)

        # Stamp the archive's filament colour from the matched Spoolman spools
        # so it reflects the curated inventory colour, not the slicer's 3MF
        # value (#1494) — mirrors the built-in inventory path in usage_tracker.
        #
        # Not from a guess (#2953): those rewrites overwrite what the slicer
        # recorded, so a spool picked by position would leave nothing to compare
        # against — the grams can be put back, the sliced colour cannot.
        if not mapping_is_guess:
            await _apply_spool_colors_to_archive(db, archive_id, filament_usage, slot_colors)

            # Same for the material: a slot mapped to a differently-typed spool than
            # it was sliced for otherwise records the sliced type (#2563).
            await _apply_spool_types_to_archive(db, archive_id, filament_usage, slot_materials)

        # Weight and cost from what was actually charged — applied whether or not
        # the slot mapping was a guess, unlike the colour and material above:
        # those overwrite what the slicer recorded, while the grams have already
        # been deducted from these spools (#2591, audit D6).
        await _settle_charges(db, archive_id, print_cost, status="completed")

        # One word for the print about filament no spool received (audit D4).
        from backend.app.services import spool_assignment_notifications

        if uncharged:
            await spool_assignment_notifications.notify_usage_not_recorded(printer_id, uncharged, db, logger)
        # Bindings the charges refuted (m266): removed now that this print's rows are no
        # longer its evidence, and said once. The removal writes in its own session.
        await db.commit()
        await identity.settle()
        if identity.changed:
            await spool_assignment_notifications.notify_usage_spool_changed(printer_id, identity.changed, db, logger)


async def _report_remain_delta_for_slots(
    client,
    *,
    printer_id: int,
    tray_remain_start: dict[str, dict],
    current_lookup: dict[str, dict],
    handled_global_tray_ids: set[int],
    archive_id: int,
    slot_colors_out: dict[int, str] | None = None,
    slot_materials_out: dict[int, str] | None = None,
    cost_out: _PrintCost | None = None,
    identity=None,
) -> int:
    """AMS remain%-delta path: write ``(start - current) * the spool's net weight``
    grams to Spoolman for slots the 3MF path didn't cover.

    ``identity`` (a ``spoolman_spool_identity.ChargeCheck``): the binding's number must
    still name the bound spool, or nothing is charged (m266).

    Mirrors ``usage_tracker.on_print_complete`` Path 2: per-slot, gated on a
    valid current ``remain%``, skipped on spool swap (``tray_uuid`` changed),
    using the resolved spool's filament reference weight rather than MQTT's
    unreliable ``tray_weight`` (which is the failure mode #1119 documented).
    """
    spools_updated = 0
    for slot_key, start in tray_remain_start.items():
        try:
            ams_id_str, tray_id_str = slot_key.split("-", 1)
            ams_id, tray_id = int(ams_id_str), int(tray_id_str)
        except (ValueError, AttributeError):
            continue

        # Skip slots already handled by the 3MF path. Encoding mirrors
        # build_ams_tray_lookup (utils/tray_ids.global_tray_id).
        global_tray_id = stored_global_tray(ams_id, tray_id)
        if global_tray_id in handled_global_tray_ids:
            continue

        current = current_lookup.get(slot_key)
        if not current:
            logger.debug("[SPOOLMAN] AMS%d-T%d: no current remain%% at completion, skipping fallback", ams_id, tray_id)
            continue

        # Spool swap mid-print — tray_uuid changed. We don't know how much
        # of the print went to which spool; skip rather than mis-attribute.
        start_uuid = (start.get("tray_uuid") or "").lower()
        cur_uuid = (current.get("tray_uuid") or "").lower()
        if start_uuid and cur_uuid and start_uuid != cur_uuid:
            logger.info(
                "[SPOOLMAN] AMS%d-T%d: spool swapped mid-print (uuid changed), skipping remain-delta", ams_id, tray_id
            )
            continue

        delta_pct = start["remain"] - current["remain"]
        if delta_pct <= 0:
            continue  # No consumption captured at AMS granularity, or refilled

        bound_spool = await _resolve_bound_spool(printer_id, ams_id, tray_id)
        if bound_spool is None:
            logger.debug("[SPOOLMAN] AMS%d-T%d: no Spoolman slot assignment, skipping fallback", ams_id, tray_id)
            continue
        spool_id, bound_stamp = bound_spool

        # Look up the spool's filament reference weight. Use a fresh GET so
        # we don't depend on a stale cached_spools list. Failure here is
        # silent-skip rather than fatal — other slots can still be written.
        try:
            spool = await client.get_spool(spool_id)
        except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
            logger.debug("[SPOOLMAN] AMS%d-T%d: get_spool(%s) failed: %s", ams_id, tray_id, spool_id, exc)
            if (
                identity is not None
                and isinstance(exc, SpoolmanNotFoundError)
                and not await identity.judge(spool_id, None, slot_stamp=bound_stamp)
            ):
                # Gone from Spoolman: said like another spool, without a figure (acceptance A03).
                identity.changed.append((global_tray_id, 0.0, spool_id))
            continue
        if identity is not None and not await identity.judge(spool_id, spool, slot_stamp=bound_stamp):
            # The grams are not worked out for a spool that is not the bound one: the
            # notice names the slot without a figure it cannot know.
            identity.changed.append((global_tray_id, 0.0, spool_id))
            continue
        filament = spool.get("filament") or {}
        # The spool's own net weight, else the catalogue's: the percentage is of
        # THIS spool, and a 250 g refill under a 1000 g filament was charged
        # four times what it fed (upstream 2f7f17a89, #3194).
        ref_weight = spool_net_weight(spool)
        if ref_weight is None:
            logger.debug(
                "[SPOOLMAN] AMS%d-T%d: spool %s has no net weight, skipping remain-delta",
                ams_id,
                tray_id,
                spool_id,
            )
            continue

        grams_used = round((delta_pct / 100.0) * ref_weight, 2)
        if grams_used <= 0:
            continue
        try:
            await client.use_spool(spool_id, grams_used)
        except (SpoolmanNotFoundError, SpoolmanClientError, SpoolmanUnavailableError) as exc:
            logger.warning(
                "[SPOOLMAN] AMS%d-T%d: use_spool(%s, %.2fg) failed: %s", ams_id, tray_id, spool_id, grams_used, exc
            )
            continue

        spools_updated += 1
        # ``spool`` is the full row fetched above for its filament weight, so
        # the price is already in hand (#2591).
        if cost_out is not None:
            cost_out.add(grams_used, spool, f"AMS{ams_id}-T{tray_id}", spool_id=spool_id)
        if slot_colors_out is not None:
            color = filament.get("color_hex")
            if color:
                # No 3MF slot_id for this path — use the AMS slot key so the
                # colour map can still be inspected by callers if needed.
                # The archive-colour rewrite (#1494) keys on 3MF slot_ids so
                # remain-delta-only prints intentionally don't participate
                # in that rewrite (matches usage_tracker's slot_id=None).
                slot_colors_out[-(global_tray_id + 1)] = color
        if slot_materials_out is not None:
            # Same negative-key convention and same non-participation in the
            # archive rewrite as the colour map above (#2563).
            material = filament.get("material")
            if isinstance(material, str) and material.strip():
                slot_materials_out[-(global_tray_id + 1)] = material.strip()
        logger.info(
            "[SPOOLMAN] Archive %s AMS%d-T%d: %.2fg via remain-delta (%d%% of %.0fg) -> spool %s",
            archive_id,
            ams_id,
            tray_id,
            grams_used,
            delta_pct,
            ref_weight,
            spool_id,
        )
    return spools_updated


async def _apply_spool_charges_to_archive(db, archive_id: int | None, charges: _PrintCost, *, status: str) -> None:
    """Tell the archive what its Spoolman charges weighed and were worth.

    **Weight** follows the internal writer's rule (``actual_filament_grams``): a
    failed print, or one with no slicer estimate (no 3MF), records what was
    actually charged; a completed print keeps its estimate. The Spoolman writer
    never did this — a no-3MF print stayed at 0 g, a failed one at the whole
    estimate (audit D6).

    **Cost** (upstream 39835437, #2591): the priced grams at their spools' own
    rates, plus the rest of the archive's weight at the farm rate — one priced
    slot out of four must not report a quarter of the cost (#1344). Nothing
    priced and the weight unchanged: whatever was recorded stands (it may carry
    a catalogue rate). Nothing priced but the weight changed: the recorded cost
    priced the old weight, so it scales with it; with none recorded, the farm
    rate — or no cost at all when there is no rate, never an invented 0.00.

    Only the print's own consumption reaches here: runout zero corrections are
    lifetime drift and are kept out, as in the internal writer.
    """
    if archive_id is None or archive_id < 0 or charges.charged_grams <= 0:
        return

    from backend.app.models.archive import PrintArchive
    from backend.app.services.filament_cost import cost_of, default_rate_per_kg
    from backend.app.services.usage_tracker import actual_filament_grams

    archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if archive is None:
        return

    before_grams = archive.filament_used_grams
    effective = actual_filament_grams(status, charges.charged_grams, before_grams)
    grams_changed = effective != (before_grams or 0)
    new_cost = archive.cost

    if charges.priced:
        total = charges.cost
        unpriced_grams = max(0.0, effective - charges.priced_grams)
        if unpriced_grams > 0:
            rate = await default_rate_per_kg(db)
            if rate > 0:
                total += (unpriced_grams / 1000.0) * rate
        if total > 0:
            new_cost = round(total, 2)
    elif grams_changed:
        if archive.cost is not None and before_grams:
            new_cost = round(archive.cost * effective / before_grams, 2)
        else:
            new_cost = cost_of(effective, await default_rate_per_kg(db))

    if not grams_changed and new_cost == archive.cost:
        if charges.unpriced and not charges.priced:
            logger.info(
                "[SPOOLMAN] Archive %s: %d charged spool(s) carry no price -- leaving the cost as recorded",
                archive_id,
                charges.unpriced,
            )
        return

    logger.info(
        "[SPOOLMAN] Archive %s: %sg -> %sg, cost %s -> %s (%d slot(s) priced from Spoolman over %.2fg)",
        archive_id,
        before_grams,
        effective,
        archive.cost,
        new_cost,
        charges.priced,
        charges.priced_grams,
    )
    archive.filament_used_grams = effective
    archive.cost = new_cost
    await db.commit()


async def _settle_charges(db, archive_id: int | None, charges: _PrintCost, *, status: str) -> None:
    """``_apply_spool_charges_to_archive``, best-effort.

    Every charge has already landed in Spoolman by the time this runs; a
    failure recording the archive's weight or cost must not turn the usage
    report into a failed one, or read as if the spools were not charged.
    """
    try:
        await _apply_spool_charges_to_archive(db, archive_id, charges, status=status)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning(
            "[SPOOLMAN] Archive %s: could not record the charged weight/cost: %s", archive_id, exc, exc_info=True
        )
    await _record_usage_history(charges)


async def _record_usage_history(charges: _PrintCost) -> None:
    """One usage-history row per charge that landed (upstream 1d86cff5a, #3407).

    The one place the history is written from: both writers settle through
    ``_settle_charges`` once per print, after every ``use_spool`` has answered.
    It only reads what ``_PrintCost.add`` observed, in a transaction of its own
    (``spoolman_usage_history.record_print``); whatever goes wrong is logged —
    the spools are charged and the archive settled either way.
    """
    if not charges.observed:
        return
    try:
        await spoolman_usage_history.record_print(
            printer_id=charges.printer_id,
            archive_id=charges.archive_id,
            status=charges.history_status,
            charges=list(charges.observed),
            source_token=charges.source_token,
        )
    except Exception as exc:  # noqa: BLE001 — history must never fail a usage report
        logger.warning(
            "[SPOOLMAN] Archive %s: could not record the usage history: %s", charges.archive_id, exc, exc_info=True
        )


async def _apply_spool_colors_to_archive(
    db,
    archive_id: int,
    filament_usage: list[dict],
    slot_colors: dict[int, str],
) -> None:
    """Overwrite an archive's ``filament_color`` with the colours of the
    Spoolman spools that fed the print (#1494).

    Per-slot, exactly like the built-in inventory path: each used slot takes its
    Spoolman spool colour when matched, else its own 3MF colour (see
    :func:`usage_tracker._archive_colors_from_spools`).
    """
    if not slot_colors:
        return

    from backend.app.models.archive import PrintArchive
    from backend.app.services.usage_tracker import (
        _archive_colors_from_spools,
        _spool_color_to_hex,
    )

    results = [{"slot_id": sid, "color": _spool_color_to_hex(hex_)} for sid, hex_ in slot_colors.items()]
    colors = _archive_colors_from_spools(filament_usage, results)
    if not colors:
        return

    archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if archive is None:
        return

    joined = ",".join(colors)
    if joined != archive.filament_color:
        logger.info(
            "[SPOOLMAN] Archive %s filament_color %r -> %r (from Spoolman spools)",
            archive_id,
            archive.filament_color,
            joined,
        )
        archive.filament_color = joined
        await db.commit()


async def _apply_spool_types_to_archive(
    db,
    archive_id: int,
    filament_usage: list[dict],
    slot_materials: dict[int, str],
) -> None:
    """Overwrite an archive's ``filament_type`` with the materials of the Spoolman
    spools that fed the print (#2563).

    Per-slot, exactly like the built-in inventory path: each used slot takes its
    Spoolman spool material when matched, else its own 3MF type (see
    :func:`usage_tracker._archive_types_from_spools`). Note the ``", "``
    separator — ``archive.py`` writes ``filament_type`` comma-SPACE joined while
    ``filament_color`` is comma-only, and the frontend material graphs split on
    ``', '``.
    """
    if not slot_materials:
        return

    from backend.app.models.archive import PrintArchive
    from backend.app.services.usage_tracker import _archive_types_from_spools

    results = [{"slot_id": sid, "material": material} for sid, material in slot_materials.items()]
    types = _archive_types_from_spools(filament_usage, results)
    if not types:
        return

    archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if archive is None:
        return

    joined = ", ".join(types)
    if joined != archive.filament_type:
        logger.info(
            "[SPOOLMAN] Archive %s filament_type %r -> %r (from Spoolman spools)",
            archive_id,
            archive.filament_type,
            joined,
        )
        archive.filament_type = joined
        await db.commit()
