"""Automatic filament consumption tracking.

Captures AMS tray remain% at print start, then computes consumption
deltas at print complete to update spool weight_used and last_used.

Primary tracking uses 3MF slicer estimates (precise per-filament data).
AMS remain% delta is the fallback for trays not covered by 3MF data.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.utils.filament_remaining import usable_remain_percent

logger = logging.getLogger(__name__)


def _decode_mqtt_mapping(mapping_raw: list | None) -> list[int] | None:
    """Decode MQTT mapping field (snow-encoded) to bamdude global tray IDs.

    The printer's MQTT mapping field is an array indexed by slicer filament slot
    (0-based). Each value uses snow encoding: ams_hw_id * 256 + local_slot.
    65535 means unmapped.

    Returns a list of bamdude global tray IDs (or -1 for unmapped), or None if
    no valid mappings found.
    """
    if not isinstance(mapping_raw, list) or not mapping_raw:
        return None

    result = []
    for value in mapping_raw:
        if not isinstance(value, int) or value >= 65535:
            result.append(-1)
            continue

        ams_hw_id = value >> 8
        slot = value & 0xFF

        if 0 <= ams_hw_id <= 3:
            # Regular AMS: sequential global ID
            result.append(ams_hw_id * 4 + (slot & 0x03))
        elif 128 <= ams_hw_id <= 135:
            # AMS-HT: global ID is the hardware ID (one slot per unit)
            result.append(ams_hw_id)
        elif ams_hw_id in (254, 255):
            # External spool
            result.append(254 if slot != 255 else 255)
        else:
            result.append(-1)

    # Only return if at least one valid mapping exists
    if all(v < 0 for v in result):
        return None

    return result


def _spool_color_to_hex(rgba: str | None) -> str | None:
    """Normalise a ``Spool.rgba`` value (``RRGGBBAA`` hex, no ``#``) to the
    ``#RRGGBB`` form archives store in ``filament_color``.

    Alpha is dropped — the archive colour list and the Color Distribution
    graph treat filament colour as opaque. Returns ``None`` for a missing or
    too-short value so the caller can fall back to the 3MF colour.
    """
    if not rgba:
        return None
    h = rgba.strip().lstrip("#")
    if len(h) < 6:
        return None
    return "#" + h[:6].upper()


def _archive_colors_from_spools(filament_usage: list[dict], results: list[dict]) -> list[str] | None:
    """Slot-ordered, de-duplicated hex colours for an archive's ``filament_color``.

    Per slot, the resolved inventory-spool colour wins; a slot with no matched
    spool (or a spool that carries no colour) falls back to that slot's own 3MF
    ``filament_colour``. Both are normalised to ``#RRGGBB`` (alpha dropped) via
    :func:`_spool_color_to_hex`, so the archive-row colour circles and the Color
    Distribution graph stay renderable and consistent.

    The slicer's 3MF carries its own ``filament_colour`` per slot — picked
    independently of the colour the user curates on the matched inventory spool.
    So an archive printed from a ``#000000`` inventory spool would otherwise show
    the slicer's near-black ``#161616``; the curated spool colour is authoritative.

    Replaces the earlier all-or-nothing behaviour: a partial match no longer
    drops the unmatched slots' colours — they keep their 3MF colour — so a print
    that mixes loaded-spool and sliced colours is represented faithfully (#1494
    follow-up). Returns ``None`` only when no used slot yields any colour.
    """
    used: list[tuple[int, str | None]] = [
        (u["slot_id"], u.get("color"))
        for u in filament_usage
        if u.get("used_g", 0) > 0 and u.get("slot_id") is not None
    ]
    if not used:
        return None

    spool_color: dict[int, str] = {}
    for r in results:
        slot_id = r.get("slot_id")
        color = r.get("color")
        if slot_id is not None and color:
            spool_color.setdefault(slot_id, color)

    ordered: list[str] = []
    for slot_id, mf_color in sorted(used, key=lambda x: x[0]):
        color = spool_color.get(slot_id) or _spool_color_to_hex(mf_color)
        if color and color not in ordered:
            ordered.append(color)
    return ordered or None


def _archive_types_from_spools(filament_usage: list[dict], results: list[dict]) -> list[str] | None:
    """Slot-ordered, de-duplicated materials for an archive's ``filament_type``.

    Per slot the resolved inventory-spool material wins; a slot with no matched
    spool (or a spool carrying no material) falls back to that slot's own 3MF
    ``type``. Exact mirror of :func:`_archive_colors_from_spools` — both fields are
    stamped from the same ``slice_info`` at archive creation and must be refined
    the same way.

    The 3MF records the type it was *sliced for*. When a slot is hand-mapped in the
    Print dialog to a differently-typed loaded spool — a PLA slice routed to the
    only loaded PETG slot — that sliced type misfiles the run in the archive card,
    the material filter and the Statistics material graphs, even though the
    deduction correctly hit the PETG spool.

    Diverges from upstream's all-or-nothing gate for the same reason the colour
    helper does: a partial match keeps the unmatched slots' sliced type instead of
    discarding every resolved material. Returns ``None`` only when no used slot
    yields any material at all.

    ``material`` is ``isinstance``-checked because the Spoolman caller feeds it
    straight from external JSON, so a non-string must never reach the callers'
    ``", ".join``.
    """
    used: list[tuple[int, str | None]] = [
        (u["slot_id"], u.get("type")) for u in filament_usage if u.get("used_g", 0) > 0 and u.get("slot_id") is not None
    ]
    if not used:
        return None

    spool_material: dict[int, str] = {}
    for r in results:
        slot_id = r.get("slot_id")
        material = r.get("material")
        if slot_id is not None and isinstance(material, str) and material.strip():
            spool_material.setdefault(slot_id, material.strip())

    ordered: list[str] = []
    for slot_id, mf_type in sorted(used, key=lambda x: x[0]):
        material = spool_material.get(slot_id) or (mf_type.strip() if isinstance(mf_type, str) else None)
        if material and material not in ordered:
            ordered.append(material)
    return ordered or None


def _match_slots_by_color(
    filament_usage: list[dict],
    ams_raw: dict | list | None,
) -> list[int] | None:
    """Match 3MF filament slots to AMS trays by color.

    Fallback mapping for printers that don't provide the MQTT mapping field
    or request topic subscription (e.g. A1, A1 Mini, P1S, P2S).

    Compares the 3MF slicer filament color (per slot) against each AMS tray's
    color to find a unique match. Only returns a mapping if every used slot
    matches exactly one tray (no ambiguity).

    Args:
        filament_usage: List of 3MF slot dicts with 'slot_id', 'color', 'type'
        ams_raw: raw_data["ams"] dict or list from printer state

    Returns:
        List of global tray IDs indexed by slicer slot (0-based), or None.
    """
    if not filament_usage or not ams_raw:
        return None

    ams_data = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
    if not ams_data:
        return None

    # Build map of normalized color → list of global tray IDs
    color_to_trays: dict[str, list[int]] = {}
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        for tray in ams_unit.get("tray", []):
            tray_id = int(tray.get("id", 0))
            tray_color = tray.get("tray_color", "")
            tray_type = tray.get("tray_type", "")
            if not tray_color or not tray_type:
                continue
            # Normalize AMS color: strip alpha (last 2 chars), lowercase
            norm = tray_color[:6].lower() if len(tray_color) >= 6 else tray_color.lower()
            if ams_id >= 128:
                global_id = ams_id  # AMS-HT
            else:
                global_id = ams_id * 4 + tray_id
            color_to_trays.setdefault(norm, []).append(global_id)

    if not color_to_trays:
        return None

    # Find max slot_id to size the result array
    max_slot = max(u.get("slot_id", 0) for u in filament_usage)
    if max_slot <= 0:
        return None

    result = [-1] * max_slot
    used_trays: set[int] = set()

    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        if slot_id <= 0:
            continue
        slot_color = usage.get("color", "").lstrip("#").lower()
        if len(slot_color) < 6:
            return None  # Can't match without a valid color

        slot_color = slot_color[:6]  # Strip alpha if present
        candidates = color_to_trays.get(slot_color, [])
        # Filter out trays already claimed by another slot
        available = [t for t in candidates if t not in used_trays]

        if len(available) != 1:
            # Ambiguous (multiple trays with same color) or no match
            return None

        result[slot_id - 1] = available[0]
        used_trays.add(available[0])

    # Only return if at least one valid mapping exists
    if all(v < 0 for v in result):
        return None

    logger.info("[UsageTracker] Color-matched slot_to_tray: %s", result)
    return result


@dataclass
class PrintSession:
    printer_id: int
    print_name: str
    started_at: datetime
    tray_remain_start: dict[tuple[int, int], int] = field(default_factory=dict)
    # RFID uuid per slot at print start — the remain-delta's spool-swap gate
    # (parity with the Spoolman delta): a changed uuid means a different
    # physical reel, and the delta must not be billed to the snapshot's spool.
    tray_uuid_start: dict[tuple[int, int], str] = field(default_factory=dict)
    # tray_now at print start (correct value, unlike at completion where it's 255)
    tray_now_at_start: int = -1
    # Snapshot of spool assignments at print start: {(ams_id, tray_id): spool_id}
    # Prevents usage loss when on_ams_change unlinks a spool mid-print
    spool_assignments: dict[tuple[int, int], int] = field(default_factory=dict)
    # Slicer slot -> global tray as DISPATCHED. Kept because the live MQTT
    # ``mapping`` field is not a substitute: AMS filament backup rewrites it to
    # the substitute tray when a spool runs dry, so read at completion it names
    # the tray that finished the print rather than the one the slicer assigned.
    ams_mapping: list[int] | None = None


# Module-level storage, keyed by printer_id. Mirrored to the
# ``active_print_sessions`` table so a restart mid-print doesn't lose the
# context — see ``persist_session`` / ``restore_session``.
_active_sessions: dict[int, PrintSession] = {}

# Serialises the read-modify-write on the persisted tray-change log, per printer.
_tray_change_locks: dict[int, asyncio.Lock] = {}


def _tray_key_to_str(key: tuple[int, int]) -> str:
    return f"{key[0]}-{key[1]}"


def _tray_key_from_str(key: str) -> tuple[int, int] | None:
    ams_str, _, tray_str = key.partition("-")
    try:
        return int(ams_str), int(tray_str)
    except ValueError:
        return None


def _tray_map_to_json(mapping: dict[tuple[int, int], int]) -> dict[str, int]:
    return {_tray_key_to_str(k): v for k, v in mapping.items()}


def _tray_map_from_json(mapping: dict | None) -> dict[tuple[int, int], int]:
    result: dict[tuple[int, int], int] = {}
    for raw_key, value in (mapping or {}).items():
        key = _tray_key_from_str(str(raw_key))
        if key is not None and isinstance(value, int):
            result[key] = value
    return result


async def persist_session(
    db: AsyncSession,
    session: PrintSession,
    tray_change_log: list | None = None,
) -> None:
    """Mirror ``session`` into ``active_print_sessions`` for restart recovery.

    Overwrites any existing row for the printer: a printer runs one print at a
    time, and a row left behind by a completion we never saw must not outlive
    the next print start.
    """
    from backend.app.models.active_print_session import ActivePrintSession

    row = await db.get(ActivePrintSession, session.printer_id)
    if row is None:
        row = ActivePrintSession(printer_id=session.printer_id)
        db.add(row)

    row.print_name = session.print_name or ""
    row.started_at = session.started_at.replace(tzinfo=None)
    row.tray_now_at_start = session.tray_now_at_start
    row.ams_mapping = list(session.ams_mapping) if session.ams_mapping else None
    row.spool_assignments = _tray_map_to_json(session.spool_assignments) or None
    # Combined per-slot value {"remain", "uuid"} when the uuid is known —
    # SAME column, richer value; readers accept the legacy bare int too.
    remain_json: dict[str, object] = {}
    for key, remain in session.tray_remain_start.items():
        uuid = session.tray_uuid_start.get(key)
        remain_json[_tray_key_to_str(key)] = {"remain": remain, "uuid": uuid} if uuid else remain
    row.tray_remain_start = remain_json or None
    row.tray_change_log = [list(entry) for entry in (tray_change_log or [])] or None

    await db.commit()


async def record_tray_change(db: AsyncSession, printer_id: int, tray_global: int, layer_num: int) -> None:
    """Append one tray change to the persisted log.

    No-op when no print-start row exists — a tray change outside a tracked
    print has nothing to attribute.
    """
    from backend.app.models.active_print_session import ActivePrintSession

    # Read-modify-write on a JSON column: two changes close together (a runout
    # parks the extruder and the backup tray loads moments later) would
    # otherwise race and drop a segment boundary.
    async with _tray_change_locks.setdefault(printer_id, asyncio.Lock()):
        row = await db.get(ActivePrintSession, printer_id)
        if row is None:
            return

        log = [list(entry) for entry in (row.tray_change_log or [])]
        entry = [tray_global, layer_num]
        if log and log[-1] == entry:
            # Print start seeds the log from PrinterState, which may already
            # hold a change this callback is also reporting.
            return
        log.append(entry)
        row.tray_change_log = log
        await db.commit()


async def record_tray_change_event(db: AsyncSession, printer_id: int, tray_global: int, layer_num: int) -> None:
    """Journal a mid-print tray change (m153 successor of ``record_tray_change``).

    Appends to ``print_usage_events`` with the assigned spool ids FROZEN at
    this moment — completion attributes segments from the row, not from a
    later lookup. Dropped when the printer has no active archive (nothing to
    anchor to) and deduped against an identical immediately-preceding entry
    (print start seeds the log from PrinterState, which may already hold the
    change this callback is reporting).
    """
    from backend.app.models.print_usage_event import EVENT_START, EVENT_TRAY_CHANGE, PrintUsageEvent
    from backend.app.services.print_usage_journal import active_archive_id, freeze_spool_ids, record_event

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return

    last = (
        (
            await db.execute(
                select(PrintUsageEvent)
                .where(
                    PrintUsageEvent.archive_id == archive_id,
                    PrintUsageEvent.event.in_([EVENT_START, EVENT_TRAY_CHANGE]),
                )
                .order_by(PrintUsageEvent.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if last is not None and last.global_tray_id == tray_global and last.layer_num == layer_num:
        return

    spool_id, spoolman_spool_id = await freeze_spool_ids(db, printer_id, tray_global)
    await record_event(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_TRAY_CHANGE,
        global_tray_id=tray_global,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )


async def _journal_print_start(db: AsyncSession, printer_id: int, state) -> None:
    """Seed the journal with the print's opening tray as an EVENT_START row."""
    from backend.app.models.print_usage_event import EVENT_START
    from backend.app.services.print_usage_journal import active_archive_id, freeze_spool_ids, record_event

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return

    seed_log = getattr(state, "tray_change_log", None) or []
    tray: int | None = None
    if seed_log:
        tray = seed_log[0][0]
    elif 0 <= getattr(state, "tray_now", 255) <= 254:
        tray = state.tray_now

    spool_id = spoolman_spool_id = None
    if tray is not None:
        spool_id, spoolman_spool_id = await freeze_spool_ids(db, printer_id, tray)
    await record_event(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=0,
        event=EVENT_START,
        global_tray_id=tray,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )


async def get_persisted_print_name(db: AsyncSession, printer_id: int) -> str | None:
    """Print name on the persisted row, for identity-checking a restored session."""
    from backend.app.models.active_print_session import ActivePrintSession

    row = await db.get(ActivePrintSession, printer_id)
    return row.print_name if row is not None else None


async def restore_session(db: AsyncSession, printer_id: int, register_active: bool = True) -> list[list[int]] | None:
    """Rebuild the in-memory session for ``printer_id`` from the persisted row.

    Returns the persisted tray-change log so the caller can put it back on
    ``PrinterState``, or None when there is nothing to restore.

    ``register_active=False`` returns the log without publishing the session to
    ``_active_sessions`` — for Spoolman users, who need the tray-change log
    restored but whose remain%-sync must not be suppressed by it (see
    ``on_print_start``).
    """
    from backend.app.models.active_print_session import ActivePrintSession

    row = await db.get(ActivePrintSession, printer_id)
    if row is None:
        return None

    started_at = row.started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    tray_remain_start: dict[tuple[int, int], int] = {}
    tray_uuid_start: dict[tuple[int, int], str] = {}
    for raw_key, value in (row.tray_remain_start or {}).items():
        key = _tray_key_from_str(str(raw_key))
        if key is None:
            continue
        if isinstance(value, dict):
            remain = value.get("remain")
            if isinstance(remain, int):
                tray_remain_start[key] = remain
            uuid = value.get("uuid")
            if isinstance(uuid, str) and uuid:
                tray_uuid_start[key] = uuid
        elif isinstance(value, int):  # legacy bare-int rows
            tray_remain_start[key] = value

    session = PrintSession(
        printer_id=printer_id,
        print_name=row.print_name or "",
        started_at=started_at,
        tray_remain_start=tray_remain_start,
        tray_uuid_start=tray_uuid_start,
        tray_now_at_start=row.tray_now_at_start,
        spool_assignments=_tray_map_from_json(row.spool_assignments),
        ams_mapping=list(row.ams_mapping) if row.ams_mapping else None,
    )
    if register_active:
        _active_sessions[printer_id] = session

    # Journal-first: the events table is the tray log's home since m153. The
    # JSON column is a one-release read fallback for a print that was already
    # running when the upgrade landed (written by the old binary).
    log: list[list[int]] = []
    try:
        from backend.app.models.print_usage_event import EVENT_START, EVENT_TRAY_CHANGE
        from backend.app.services.print_usage_journal import active_archive_id, load_events

        archive_id = await active_archive_id(db, printer_id)
        if archive_id is not None:
            events = await load_events(db, printer_id, archive_id)
            log = [
                [e.global_tray_id, e.layer_num]
                for e in events
                if e.event in (EVENT_START, EVENT_TRAY_CHANGE) and e.global_tray_id is not None
            ]
    except Exception:
        logger.exception("[UsageTracker] Journal read failed during restore for printer %d", printer_id)
    if not log:
        log = [list(entry) for entry in (row.tray_change_log or [])]
    logger.info(
        "[UsageTracker] Restored print session for printer %d: ams_mapping=%s, %d assignments, tray_change_log=%s",
        printer_id,
        row.ams_mapping,
        len(row.spool_assignments or {}),
        log,
    )
    return log


async def clear_persisted_session(db: AsyncSession, printer_id: int) -> None:
    """Drop the persisted print-start row once the print is closed out."""
    from backend.app.models.active_print_session import ActivePrintSession

    row = await db.get(ActivePrintSession, printer_id)
    if row is not None:
        await db.delete(row)
        await db.commit()


async def discard_session(db: AsyncSession, printer_id: int) -> None:
    """Forget a printer's print-start context, in memory and on disk.

    The completion path calls this for every print, including the ones whose
    usage Spoolman owns: the context is captured for both backends, but only
    the internal tracker's ``on_print_complete`` consumes (and pops) it.
    """
    _active_sessions.pop(printer_id, None)
    _tray_change_locks.pop(printer_id, None)
    await clear_persisted_session(db, printer_id)


def _to_epoch_seconds(value: datetime | None) -> float | None:
    """Convert datetime to epoch seconds, assuming UTC for naive values."""
    if value is None:
        return None
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def actual_filament_grams(status: str, tracked_grams: float, estimate: float | None) -> float:
    """Filament weight to store on the archive for a finished print.

    At archive creation ``filament_used_grams`` holds the full slicer estimate.
    For a partial / failed print only a fraction was extruded, so the estimate
    over-counts — stats would then disagree with inventory (which was deducted by
    the *actual* tracked usage). When usage was tracked, substitute the actually
    consumed weight; otherwise (completed, or a failure we couldn't measure) keep
    the estimate, which equals actual at 100%.
    """
    if status != "completed" and tracked_grams > 0:
        return round(tracked_grams, 1)
    return estimate or 0.0


# Same default the Inventory page uses when the global setting is unset, so a
# spool the UI paints as low is exactly the one that warns.
_DEFAULT_LOW_STOCK_THRESHOLD = 20.0


def _global_tray_id(ams_id: int, tray_id: int) -> int:
    """``(ams_id, tray_id)`` → global tray ID.

    The exact inverse of the decomposition this module already does in the 3MF
    paths, and it is not ``ams_id * 4 + tray_id`` across the board: an AMS-HT
    unit holds one spool and *is* its own global ID, and the external spools sit
    at 254/255 behind a sentinel ams_id. Getting this wrong doesn't fail loudly —
    it just labels an HT spool as some nonexistent slot in the warning.
    """
    if ams_id >= 254:
        return 254 + tray_id
    if ams_id >= 128:
        return ams_id
    return ams_id * 4 + tray_id


async def _global_low_stock_threshold(db: AsyncSession) -> float:
    from backend.app.models.settings import Settings

    raw = (await db.execute(select(Settings.value).where(Settings.key == "low_stock_threshold"))).scalar_one_or_none()
    if raw is None:
        return _DEFAULT_LOW_STOCK_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LOW_STOCK_THRESHOLD


async def _warn_if_low_stock(db: AsyncSession, spool: Spool, printer_id: int, global_tray_id: int) -> None:
    """Fire ``filament_low`` once per run-down, right after consumption lands.

    Called from every path that writes ``weight_used``, straight after the write,
    so the number in the message is the one now stored. It is safe to call more
    than once for the same spool in one pass — the flag makes every call after
    the first a no-op.

    The arithmetic deliberately mirrors the Inventory page exactly (remaining =
    ``label_weight - weight_used`` clamped at 0, strict ``<`` against the
    per-spool override or the global setting, archived spools ignored). A
    notification that disagrees with what the page shows is worse than none.
    """
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    # The one place slot labels are built (A1 / Ext-L / HT-A / Lite-3). Imported
    # rather than re-derived — a second numbering scheme in notifications is how
    # "slot 3" comes to mean two different trays.
    from backend.app.services.spool_assignment_notifications import _slot_label_from_global_tray

    if spool.archived_at is not None:
        return
    label = spool.label_weight or 0
    if label <= 0:
        return

    remaining = max(0.0, label - (spool.weight_used or 0))
    remaining_pct = remaining / label * 100.0
    threshold = spool.low_stock_threshold_pct
    if threshold is None:
        threshold = await _global_low_stock_threshold(db)

    if remaining_pct >= threshold:
        # Back above the line — a refill, or a usage reset. Re-arm so the next
        # run-down is announced.
        spool.low_stock_notified = False
        return
    if spool.low_stock_notified:
        return

    spool.low_stock_notified = True
    printer_name = (
        await db.execute(select(Printer.name).where(Printer.id == printer_id))
    ).scalar_one_or_none() or "Unknown"
    try:
        await notification_service.on_filament_low(
            printer_id=printer_id,
            printer_name=printer_name,
            slot=_slot_label_from_global_tray(global_tray_id),
            remaining_percent=int(remaining_pct),
            db=db,
            color=spool.color_name,
        )
    except Exception:
        # Tracking consumption is the job; announcing it is not allowed to lose
        # the write that just happened.
        logger.exception("[UsageTracker] filament_low notification failed for spool %s", spool.id)


# The status an AMS-sync correction carries in ``spool_usage_history``. Distinct
# from the print outcomes (completed/failed/aborted/cancelled) because it is not
# a print — it is filament this instance never saw leave the spool.
AMS_SYNC_STATUS = "ams_sync"

# The status a runout zero-correction carries: the tail of a spool our books
# never saw leave it, written when a detected runout proves the spool holds
# exactly 0 g. Not a print either — the print's own segment rows are separate.
RUNOUT_STATUS = "runout"


def journal_touched_trays(events: list) -> set[int]:
    """Every tray the journal names — excluded from the remain%-delta path.

    Belt-and-braces against the multicolour double-count: a substitute tray
    that fed part of the print appears here (tray_change / runout /
    spool_loaded), so Path 2 must not charge its remain-delta on top of
    whatever Path 1 attributed."""
    return {e.global_tray_id for e in (events or []) if e.global_tray_id is not None}


def journal_boundaries_for_tray(events: list, global_tray_id: int) -> list[tuple[int, int | None, int | None]]:
    """Spool-change boundaries for ONE tray: ``[(start_layer, spool_id, spoolman_id)]``.

    Handles MULTIPLE runout episodes of the same tray in one print (two short
    reels back-to-back is real). Per episode, in event order: the origin runs
    to the runout layer; the follow-on feeder is the same-tray ``spool_loaded``
    (refill), the backup tray's frozen spool (autoswitch: the first tray_change
    to ANOTHER tray at the runout layer), or ``None`` when it can't be named
    (charged to nothing rather than guessed). An ``ambiguous`` runout is a
    boundary only when a replacement was demonstrably loaded; a runout with no
    follow-on keeps its own spool — the zero correction closes it at
    label_weight regardless. Empty/single-segment results mean "no split".
    """
    from backend.app.models.print_usage_event import (
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        EVENT_TRAY_CHANGE,
        KIND_AMBIGUOUS,
        KIND_AUTOSWITCH,
    )

    runouts = [e for e in (events or []) if e.event == EVENT_RUNOUT and e.global_tray_id == global_tray_id]
    if not runouts:
        return []

    loads = [e for e in events if e.event == EVENT_SPOOL_LOADED and e.global_tray_id == global_tray_id]

    segments: list[tuple[int, int | None, int | None]] = [(0, runouts[0].spool_id, runouts[0].spoolman_spool_id)]
    for idx, runout in enumerate(runouts):
        next_runout_id = runouts[idx + 1].id if idx + 1 < len(runouts) else None
        loaded = next(
            (e for e in loads if e.id > runout.id and (next_runout_id is None or e.id < next_runout_id)),
            None,
        )
        if runout.kind == KIND_AMBIGUOUS:
            # Could equally be a jam — a boundary only when the human
            # demonstrably loaded a replacement.
            if loaded is not None:
                segments.append((runout.layer_num, loaded.spool_id, loaded.spoolman_spool_id))
            continue
        if loaded is not None:
            segments.append((runout.layer_num, loaded.spool_id, loaded.spoolman_spool_id))
        elif runout.kind == KIND_AUTOSWITCH:
            backup = next(
                (
                    e
                    for e in events
                    if e.id > runout.id
                    and e.event == EVENT_TRAY_CHANGE
                    and e.global_tray_id is not None
                    and e.global_tray_id != global_tray_id
                    and e.layer_num <= runout.layer_num + 1
                ),
                None,
            )
            if backup is not None:
                segments.append((runout.layer_num, backup.spool_id, backup.spoolman_spool_id))
            else:
                # The backup feeder can't be named — better an under-count on
                # the backup spool than grams on a guess; the origin zeroes out.
                segments.append((runout.layer_num, None, None))
        else:
            # Resumed without a detectable replacement — the same spool (or a
            # splice) kept feeding; the zero correction self-consistently
            # closes it at label_weight after the print rows.
            segments.append((runout.layer_num, runout.spool_id, runout.spoolman_spool_id))

    return segments if len(segments) > 1 else []


def _add_autoswitch_purge(
    segments: list[tuple[int, int, float]],
    tray_changes: list[tuple[int, int]],
    events: list,
    purge_grams: float,
) -> list[tuple[int, int, float]]:
    """Add the emergency-swap purge to each autoswitch backup segment.

    The slicer's estimate contains the *planned* colour-change flushes but not
    the purge of an AMS backup switch — that filament is physically fed by the
    backup spool, so it lands inside the backup segment's grams (never a
    separate row). Coarse by nature; ``runout_purge_grams`` defaults to 0.
    """
    from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_AUTOSWITCH

    if purge_grams <= 0 or not events or not segments:
        return segments
    runouts = [
        e for e in events if e.event == EVENT_RUNOUT and e.kind == KIND_AUTOSWITCH and e.global_tray_id is not None
    ]
    if not runouts:
        return segments

    out = [list(s) for s in segments]
    for runout in runouts:
        for seg_idx, tray_global, _grams in segments:
            seg_layer = tray_changes[seg_idx][1]
            if tray_global != runout.global_tray_id and abs(seg_layer - runout.layer_num) <= 1:
                out[seg_idx][2] += purge_grams
                break
    return [tuple(s) for s in out]


def _autoswitch_purge_for_tray(events: list, global_tray_id: int, purge_grams: float) -> float:
    """The purge grams owed to a journal-split backup segment on this tray."""
    from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_AUTOSWITCH

    if purge_grams <= 0 or not events:
        return 0.0
    for e in events:
        if e.event == EVENT_RUNOUT and e.kind == KIND_AUTOSWITCH and e.global_tray_id == global_tray_id:
            return purge_grams
    return 0.0


async def _zero_point_enabled(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, "runout_zero_point_enabled")
    return raw is None or raw.lower() != "false"


async def apply_runout_zero_corrections(
    db: AsyncSession,
    printer_id: int,
    events: list,
    default_filament_cost: float,
) -> list[dict]:
    """Close every unambiguously-run-out spool at exactly label_weight.

    Runs AFTER all print rows so the arithmetic is one subtraction: ``tail =
    label_weight − weight_used``. A positive tail is drift the books never saw
    — it gets a ``runout`` history row (readers SUM the table, so the spool's
    history now adds up to the label). A negative tail means the books
    over-counted; per the AMS-sync rule a downward move is a correction, not a
    negative print: silent clamp, baseline pulled, low-stock re-armed, no row.

    Never fires for ``ambiguous`` kinds (a tangled spool people swap out is
    not empty) or for events whose slot/spool was not positively known.
    """
    from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_AMBIGUOUS

    runouts = [
        e
        for e in (events or [])
        if e.event == EVENT_RUNOUT and e.kind != KIND_AMBIGUOUS and e.global_tray_id is not None and e.spool_id
    ]
    if not runouts:
        return []
    if not await _zero_point_enabled(db):
        return []

    results: list[dict] = []
    touched = False
    for event in runouts:
        spool = (await db.execute(select(Spool).where(Spool.id == event.spool_id))).scalar_one_or_none()
        if spool is None:
            continue
        label = spool.label_weight or 0
        if label <= 0:
            continue
        tail = round(label - (spool.weight_used or 0), 1)
        if tail > 0:
            spool.weight_used = float(label)
            spool.last_used = datetime.now(timezone.utc)
            cost = None
            cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
            if cost_per_kg > 0:
                cost = round((tail / 1000.0) * cost_per_kg, 2)
            db.add(
                SpoolUsageHistory(
                    spool_id=spool.id,
                    printer_id=printer_id,
                    print_name=None,
                    weight_used=tail,
                    percent_used=int(round(tail / label * 100)),
                    status=RUNOUT_STATUS,
                    cost=cost,
                    archive_id=event.archive_id,
                )
            )
            await _warn_if_low_stock(db, spool, printer_id, event.global_tray_id)
            results.append(
                {
                    "spool_id": spool.id,
                    "weight_used": tail,
                    "percent_used": int(round(tail / label * 100)),
                    "ams_id": event.global_tray_id // 4 if event.global_tray_id < 128 else 255,
                    "tray_id": event.global_tray_id % 4 if event.global_tray_id < 128 else 0,
                    "material": spool.material,
                    "cost": cost,
                    "slot_id": None,
                    "color": _spool_color_to_hex(spool.rgba),
                    "status": RUNOUT_STATUS,
                }
            )
            touched = True
            logger.info(
                "[UsageTracker] Runout zero-point: spool %d closed at label %dg (+%.1fg tail) on printer %d tray %d",
                spool.id,
                label,
                tail,
                printer_id,
                event.global_tray_id,
            )
        elif tail < 0:
            spool.weight_used = float(label)
            if (spool.weight_used_baseline or 0) > label:
                spool.weight_used_baseline = float(label)
            spool.low_stock_notified = False
            touched = True
            logger.info(
                "[UsageTracker] Runout zero-point: spool %d clamped down to label %dg (books had %.1fg over)",
                spool.id,
                label,
                -tail,
            )
    if touched:
        await db.commit()
    return results


async def record_ams_sync_usage(
    db: AsyncSession,
    spool: Spool,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    new_used: float,
) -> float:
    """Move a spool's ``weight_used`` to the AMS reading, and leave a row behind.

    Both AMS-sync sites — the live one in ``main.on_ams_change`` and the manual
    ``POST /inventory/sync-ams-weights`` — go through here, so the rule cannot
    drift between them the way the grams conversion once did.

    ⚠️ **This is the one write path that used to move the books in silence.**
    Every other writer of ``weight_used`` (the 3MF path, the layer path, the
    remain%-delta fallback) adds a ``SpoolUsageHistory`` row beside the number,
    which is why a discrepancy between them is meaningful at all — and why an
    AMS sync that skipped the row was invisible until someone summed the history
    by hand and found 154 g of prints against a spool the page called spent.

    An **increase** is genuine consumption this instance did not witness — a job
    started from the touchscreen, a purge, a spool moved between printers — so it
    earns a row and feeds the forecast like any other. A **decrease** is a
    correction of our own books, not a negative print, and gets no row: every
    reader of this table SUMs it, and a negative row would quietly subtract from
    farm-wide consumption. It re-arms the low-stock warning instead, because a
    spool that was topped up and then burnt back down in one print would
    otherwise stay silent (m117).

    Returns the delta in grams — positive when filament went missing.
    """
    old_used = float(spool.weight_used or 0.0)
    delta = round(new_used - old_used, 1)
    spool.weight_used = new_used

    if delta <= 0:
        if (spool.weight_used_baseline or 0) > new_used:
            spool.weight_used_baseline = new_used
        spool.low_stock_notified = False
        return delta

    label = spool.label_weight or 0
    spool.last_used = datetime.now(timezone.utc)
    db.add(
        SpoolUsageHistory(
            spool_id=spool.id,
            printer_id=printer_id,
            print_name=None,
            weight_used=delta,
            percent_used=int(round(delta / label * 100)) if label > 0 else 0,
            status=AMS_SYNC_STATUS,
            cost=None,
            archive_id=None,
        )
    )
    await _warn_if_low_stock(db, spool, printer_id, _global_tray_id(ams_id, tray_id))
    logger.info(
        "[UsageTracker] Spool %d reconciled +%.1fg from AMS reading on printer %d AMS%d-T%d (%s -> %s)",
        spool.id,
        delta,
        printer_id,
        ams_id,
        tray_id,
        round(old_used, 1),
        new_used,
    )
    return delta


async def _resolve_spool_id_for_tray(
    printer_id: int,
    ams_id: int,
    tray_id: int,
    db: AsyncSession,
    spool_assignments_snapshot: dict[tuple[int, int], int] | None = None,
    print_started_at: datetime | None = None,
) -> int | None:
    """Resolve spool ID for a tray with safe support for mid-print reassignment.

    Resolution order:
    1. If snapshot exists and live assignment changed *during this print*, use live spool.
    2. Otherwise use snapshot spool when available.
    3. Fall back to live assignment.
    """
    key = (ams_id, tray_id)
    snapshot_spool_id = spool_assignments_snapshot.get(key) if spool_assignments_snapshot else None

    # Backward-compatible fast path: if we have a snapshot but no print-start
    # timestamp, preserve legacy behavior and avoid extra DB lookups.
    if snapshot_spool_id is not None and print_started_at is None:
        return snapshot_spool_id

    result = await db.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    live_assignment = result.scalar_one_or_none()

    if snapshot_spool_id is not None:
        if live_assignment and live_assignment.spool_id != snapshot_spool_id:
            live_created_ts = _to_epoch_seconds(getattr(live_assignment, "created_at", None))
            started_ts = _to_epoch_seconds(print_started_at)
            if live_created_ts is not None and started_ts is not None and live_created_ts >= started_ts:
                logger.info(
                    "[UsageTracker] Assignment changed during print for printer %d AMS%d-T%d: snapshot spool %d -> live spool %d",
                    printer_id,
                    ams_id,
                    tray_id,
                    snapshot_spool_id,
                    live_assignment.spool_id,
                )
                return live_assignment.spool_id
        return snapshot_spool_id

    if live_assignment:
        return live_assignment.spool_id

    return None


async def on_print_start(
    printer_id: int,
    data: dict,
    printer_manager,
    db: AsyncSession | None = None,
    spoolman_owns_usage: bool = False,
) -> None:
    """Capture AMS tray remain% and spool assignments at print start.

    The capture runs for **both** inventory backends. The persisted row carries
    the tray-change log, which is the only record of which spool fed which
    layers when AMS filament backup swaps trays, and Spoolman's own durable row
    (#1820) does not hold it — capturing on one side only would leave Spoolman
    users with the mid-print-restart attribution bug this fixes for everyone
    else.

    ``spoolman_owns_usage`` keeps the in-memory session out of
    ``_active_sessions``. That dict doubles as ``on_ams_change``'s "a print is
    running, so skip the remain%-based weight sync because the internal tracker
    will deduct precisely" flag; registering a session the internal tracker will
    never complete would suppress a sync those users still need.
    """
    state = printer_manager.get_status(printer_id)
    if not state or not state.raw_data:
        logger.debug("[UsageTracker] No state for printer %d, skipping", printer_id)
        return

    ams_raw = state.raw_data.get("ams", [])
    ams_data = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
    if not ams_data:
        logger.debug("[UsageTracker] No AMS data for printer %d, skipping", printer_id)
        return

    tray_remain_start: dict[tuple[int, int], int] = {}
    tray_uuid_start: dict[tuple[int, int], str] = {}
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        for tray in ams_unit.get("tray", []):
            tray_id = int(tray.get("id", 0))
            remain = usable_remain_percent(tray.get("remain"))
            if remain is not None:
                tray_remain_start[(ams_id, tray_id)] = remain
                uuid = str(tray.get("tray_uuid", "") or "")
                if uuid and set(uuid) != {"0"}:
                    tray_uuid_start[(ams_id, tray_id)] = uuid

    print_name = data.get("subtask_name", "") or data.get("filename", "unknown")

    # Capture tray_now at print start (reliable, unlike at completion where it's 255)
    tray_now_at_start = state.tray_now if state else -1

    # --- Diagnostic logging: dump mapping-related MQTT fields at print start ---
    # This helps us understand what each printer model reports for slot-to-tray mapping.
    mapping_field = state.raw_data.get("mapping")
    logger.info(
        "[UsageTracker] PRINT START printer %d: mapping=%s, tray_now=%d, last_loaded_tray=%s",
        printer_id,
        mapping_field,
        tray_now_at_start,
        getattr(state, "last_loaded_tray", "N/A"),
    )
    # Log all raw_data keys containing "map" or "ams" for discovery
    map_keys = {k: state.raw_data[k] for k in state.raw_data if "map" in k.lower()}
    if map_keys:
        logger.info("[UsageTracker] PRINT START printer %d: mapping-related keys: %s", printer_id, map_keys)
    # Log per-tray summary: tray_now, tray_tar, tray_type, tray_color for each slot
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        tray_summary = []
        for tray in ams_unit.get("tray", []):
            tray_summary.append(
                f"T{tray.get('id', '?')}(type={tray.get('tray_type', '')}, "
                f"color={tray.get('tray_color', '')}, "
                f"now={ams_raw.get('tray_now', '?') if isinstance(ams_raw, dict) else '?'}, "
                f"tar={ams_raw.get('tray_tar', '?') if isinstance(ams_raw, dict) else '?'})"
            )
        logger.info("[UsageTracker] PRINT START printer %d AMS %d: %s", printer_id, ams_id, ", ".join(tray_summary))

    # Snapshot spool assignments so usage isn't lost if on_ams_change unlinks mid-print
    spool_assignments: dict[tuple[int, int], int] = {}
    if db:
        assign_result = await db.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer_id))
        for assignment in assign_result.scalars().all():
            spool_assignments[(assignment.ams_id, assignment.tray_id)] = assignment.spool_id
        if spool_assignments:
            logger.info(
                "[UsageTracker] Snapshotted %d spool assignments for printer %d: %s",
                len(spool_assignments),
                printer_id,
                {f"{k[0]}-{k[1]}": v for k, v in spool_assignments.items()},
            )

    # Always create session (even without valid remain data) so print_name
    # is available at completion for 3MF-based tracking
    session = PrintSession(
        printer_id=printer_id,
        print_name=print_name,
        started_at=datetime.now(timezone.utc),
        tray_remain_start=tray_remain_start,
        tray_uuid_start=tray_uuid_start,
        tray_now_at_start=tray_now_at_start,
        spool_assignments=spool_assignments,
        ams_mapping=data.get("ams_mapping"),
    )
    if spoolman_owns_usage:
        _active_sessions.pop(printer_id, None)
    else:
        _active_sessions[printer_id] = session

    # Mirror to the DB so a restart mid-print doesn't lose the context. The
    # tray log itself lives in ``print_usage_events`` since m153 — the session
    # row keeps only context, and its legacy ``tray_change_log`` column is
    # cleared here so a stale value can never shadow the journal. The seed
    # tray (bambu_mqtt cleared + reseeded the state log before this callback)
    # becomes the journal's EVENT_START row.
    if db:
        try:
            await persist_session(db, session)
            await _journal_print_start(db, printer_id, state)
        except Exception:
            logger.exception("[UsageTracker] Failed to persist print session for printer %d", printer_id)

    if tray_remain_start:
        logger.info(
            "[UsageTracker] Captured start remain%% for printer %d (%d trays): %s",
            printer_id,
            len(tray_remain_start),
            {f"{k[0]}-{k[1]}": v for k, v in tray_remain_start.items()},
        )
    else:
        logger.debug("[UsageTracker] No valid remain%% for printer %d, 3MF fallback available", printer_id)


async def on_print_complete(
    printer_id: int,
    data: dict,
    printer_manager,
    db: AsyncSession,
    archive_id: int | None = None,
    ams_mapping: list[int] | None = None,
) -> list[dict]:
    """Compute consumption deltas and update spool weight_used/last_used.

    Uses two tracking strategies in priority order:
    1. 3MF per-filament estimates (primary) - precise slicer data for all spools
    2. AMS remain% delta (fallback) - only for trays not already handled by 3MF

    Returns a list of dicts describing what was logged (for WebSocket broadcast).
    """
    from sqlalchemy import select

    from backend.app.api.routes.settings import get_setting
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    session = _active_sessions.pop(printer_id, None)
    if session is None:
        # Restart mid-print: the in-memory session is gone but the print-start
        # row survived. Without this the completion path loses the dispatched
        # mapping and the assignment snapshot, and attributes the whole print to
        # whichever tray happened to finish it.
        try:
            await restore_session(db, printer_id)
        except Exception:
            logger.exception("[UsageTracker] Failed to restore print session for printer %d", printer_id)
        session = _active_sessions.pop(printer_id, None)

    # The caller's mapping comes from the MQTT request-topic capture and the
    # in-memory ``_print_ams_mappings`` dict, both of which a restart destroys.
    if not ams_mapping and session and session.ams_mapping:
        ams_mapping = session.ams_mapping

    status = data.get("status", "completed")
    results = []
    handled_trays: set[tuple[int, int]] = set()

    # Fetch default filament cost from settings for fallback
    default_cost_str = await get_setting(db, "default_filament_cost")
    default_filament_cost = float(default_cost_str) if default_cost_str else 0.0

    # Optional emergency-swap purge (grams per autoswitch runout, default 0).
    _purge_str = await get_setting(db, "runout_purge_grams")
    try:
        runout_purge_grams = float(_purge_str) if _purge_str else 0.0
    except ValueError:
        runout_purge_grams = 0.0

    logger.info(
        "[UsageTracker] on_print_complete: printer=%d, archive=%s, session=%s, ams_mapping=%s",
        printer_id,
        archive_id,
        "yes" if session else "no",
        ams_mapping,
    )

    # --- Diagnostic logging: dump mapping-related MQTT fields at print completion ---
    state = printer_manager.get_status(printer_id)
    if state and state.raw_data:
        logger.info(
            "[UsageTracker] PRINT COMPLETE printer %d: mapping=%s, tray_now=%s, last_loaded_tray=%s",
            printer_id,
            state.raw_data.get("mapping"),
            state.tray_now,
            getattr(state, "last_loaded_tray", "N/A"),
        )

    # The print's journal (m153): runout/spool-change boundaries with spool
    # ids frozen at event time. With events present, the assignment snapshot
    # wins unconditionally over a live reassignment — the journal owns
    # mid-print changes, so "live wins" keeps only its wrong-assignment-
    # correction case on journal-less prints (print_started_at=None routes
    # _resolve_spool_id_for_tray onto its snapshot fast path).
    journal_events: list = []
    if archive_id:
        try:
            from backend.app.services.print_usage_journal import load_events

            journal_events = await load_events(db, printer_id, archive_id)
        except Exception:
            logger.exception("[UsageTracker] Journal load failed for archive %s", archive_id)
    # "Live assignment wins" is suspended only when the journal holds
    # BOUNDARY events (runout / spool_loaded) — those own mid-print changes.
    # Any print has a start row (and jams leave pause rows), so keying on
    # "any journal row" would kill the legitimate wrong-link correction for
    # every print; a jam-time spool swap stays on the old live-wins semantics.
    from backend.app.models.print_usage_event import EVENT_RUNOUT as _EV_RUNOUT, EVENT_SPOOL_LOADED as _EV_LOADED

    has_boundary_events = any(e.event in (_EV_RUNOUT, _EV_LOADED) for e in journal_events)
    effective_started_at = None if has_boundary_events else (session.started_at if session else None)

    # --- Path 1 (PRIMARY): 3MF per-filament estimates ---
    if archive_id:
        print_name = (
            (session.print_name if session else None) or data.get("subtask_name", "") or data.get("filename", "unknown")
        )
        threemf_results = await _track_from_3mf(
            printer_id,
            archive_id,
            status,
            print_name,
            handled_trays,
            printer_manager,
            db,
            ams_mapping=ams_mapping,
            tray_now_at_start=session.tray_now_at_start if session else -1,
            last_progress=data.get("last_progress", 0.0),
            last_layer_num=data.get("last_layer_num", 0),
            default_filament_cost=default_filament_cost,
            spool_assignments=session.spool_assignments if session else None,
            print_started_at=effective_started_at,
            journal_events=journal_events,
            runout_purge_grams=runout_purge_grams,
        )
        results.extend(threemf_results)

    # Belt-and-braces against the multicolour runout double-count: every tray
    # the journal names is off-limits to the remain%-delta fallback, whether
    # or not Path 1 managed to attribute it.
    for tray in journal_touched_trays(journal_events):
        if tray >= 254:
            handled_trays.add((255, tray - 254))
        elif tray >= 128:
            handled_trays.add((tray, 0))
        else:
            handled_trays.add((tray // 4, tray % 4))

    # --- Path 2 (FALLBACK): AMS remain% delta (only for trays not handled by 3MF) ---
    if session and session.tray_remain_start:
        state = printer_manager.get_status(printer_id)
        if state and state.raw_data:
            ams_raw = state.raw_data.get("ams", [])
            ams_data = (
                ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
            )

            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                for tray in ams_unit.get("tray", []):
                    tray_id = int(tray.get("id", 0))
                    key = (ams_id, tray_id)

                    if key in handled_trays:
                        continue  # Already tracked via 3MF

                    if key not in session.tray_remain_start:
                        continue

                    # ⚠️ A zero here is refused, not read as "empty" — see
                    # ``usable_remain_percent``. Taking it at face value charged
                    # ``start - 0``: up to the whole reel, on the sentinel the
                    # firmware emits whenever it has nothing to report.
                    current_remain = usable_remain_percent(tray.get("remain"))
                    if current_remain is None:
                        continue

                    # Spool swap mid-print — the RFID uuid changed, so this is
                    # a different physical reel and the delta belongs to nobody
                    # we can name (parity with the Spoolman remain-delta gate).
                    start_uuid = session.tray_uuid_start.get(key, "")
                    cur_uuid = str(tray.get("tray_uuid", "") or "")
                    if start_uuid and cur_uuid and set(cur_uuid) != {"0"} and start_uuid != cur_uuid:
                        logger.info(
                            "[UsageTracker] AMS%d-T%d: spool swapped mid-print (uuid changed), skipping remain-delta",
                            ams_id,
                            tray_id,
                        )
                        continue

                    start_remain = session.tray_remain_start[key]
                    delta_pct = start_remain - current_remain

                    if delta_pct <= 0:
                        continue  # No consumption or tray was refilled

                    spool_id = await _resolve_spool_id_for_tray(
                        printer_id=printer_id,
                        ams_id=ams_id,
                        tray_id=tray_id,
                        db=db,
                        spool_assignments_snapshot=session.spool_assignments,
                        print_started_at=effective_started_at,
                    )
                    if spool_id is None:
                        continue

                    # Load spool
                    spool_result = await db.execute(select(Spool).where(Spool.id == spool_id))
                    spool = spool_result.scalar_one_or_none()
                    if not spool:
                        continue

                    # Compute weight consumed
                    weight_grams = (delta_pct / 100.0) * spool.label_weight

                    # Update spool
                    spool.weight_used = (spool.weight_used or 0) + weight_grams
                    spool.last_used = datetime.now(timezone.utc)
                    await _warn_if_low_stock(db, spool, printer_id, _global_tray_id(ams_id, tray_id))

                    # Calculate cost for this usage
                    cost = None
                    cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
                    if cost_per_kg > 0:
                        cost = round((weight_grams / 1000.0) * cost_per_kg, 2)

                    # Insert usage history record
                    history = SpoolUsageHistory(
                        spool_id=spool.id,
                        printer_id=printer_id,
                        print_name=session.print_name,
                        weight_used=round(weight_grams, 1),
                        percent_used=delta_pct,
                        status=status,
                        cost=cost,
                        archive_id=archive_id,
                    )
                    db.add(history)

                    handled_trays.add(key)
                    results.append(
                        {
                            "spool_id": spool.id,
                            "weight_used": round(weight_grams, 1),
                            "percent_used": delta_pct,
                            "ams_id": ams_id,
                            "tray_id": tray_id,
                            "material": spool.material,
                            "cost": cost,
                            # AMS remain%-delta fallback has no 3MF slot — slot_id
                            # stays None so it is excluded from the colour rewrite.
                            "slot_id": None,
                            "color": _spool_color_to_hex(spool.rgba),
                        }
                    )

                    logger.info(
                        "[UsageTracker] Spool %d consumed %.1fg (%d%%) on printer %d AMS%d-T%d (AMS fallback, %s)",
                        spool.id,
                        weight_grams,
                        delta_pct,
                        printer_id,
                        ams_id,
                        tray_id,
                        status,
                    )

    if results:
        await db.commit()

    # --- Update PrintArchive.cost from THIS print session only ---
    #
    # Cover any filament weight that wasn't tracked by an inventory spool with
    # the global default rate (#1344). Without this, a multi-color print where
    # only some AMS trays are mapped to inventory spools would record only the
    # mapped slots' share — e.g. $0.01 for a 110 g print when 3 of 4 trays had
    # no spool record. archive.py sets a correct whole-print cost initially
    # (total grams × primary cost_per_kg), but this block overwrites it, so the
    # overwrite must reconstruct the whole-print cost.

    if archive_id and results:
        from sqlalchemy import select

        from backend.app.models.archive import PrintArchive

        archive_result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
        archive = archive_result.scalar_one_or_none()
        if archive:
            total_cost = sum(r.get("cost", 0) or 0 for r in results)
            tracked_grams = sum(r.get("weight_used", 0) or 0 for r in results)
            # Effective weight = estimate for completed prints, actual tracked
            # weight for partial / failed prints (see actual_filament_grams). Both
            # the untracked-cost calc and the persisted weight use it so stats and
            # cost agree with inventory. For a completed multi-color print where
            # some AMS trays aren't mapped to inventory spools, effective ==
            # estimate so untracked filament (#1344) is still costed at the default
            # rate; for a failure effective == tracked so no phantom untracked cost.
            effective_grams = actual_filament_grams(status, tracked_grams, archive.filament_used_grams)
            untracked_grams = max(0.0, effective_grams - tracked_grams)
            if untracked_grams > 0 and default_filament_cost > 0:
                total_cost += (untracked_grams / 1000.0) * default_filament_cost
            if total_cost > 0:
                archive.cost = round(total_cost, 2)
            if effective_grams != (archive.filament_used_grams or 0):
                archive.filament_used_grams = effective_grams
            await db.commit()

    # --- Runout zero corrections, AFTER every print row and the archive cost ---
    # The tail is lifetime drift, not this print's consumption: it must not
    # inflate the archive's cost/weight above, but it is broadcast so the UI
    # sees the spool close out.
    try:
        correction_results = await apply_runout_zero_corrections(db, printer_id, journal_events, default_filament_cost)
        results.extend(correction_results)
    except Exception:
        logger.exception("[UsageTracker] Runout zero corrections failed for printer %d", printer_id)

    return results


async def _track_from_3mf(
    printer_id: int,
    archive_id: int,
    status: str,
    print_name: str,
    handled_trays: set[tuple[int, int]],
    printer_manager,
    db: AsyncSession,
    ams_mapping: list[int] | None = None,
    tray_now_at_start: int = -1,
    last_progress: float = 0.0,
    last_layer_num: int = 0,
    default_filament_cost: float = 0.0,
    spool_assignments: dict[tuple[int, int], int] | None = None,
    print_started_at: datetime | None = None,
    journal_events: list | None = None,
    runout_purge_grams: float = 0.0,
) -> list[dict]:
    """Track usage from 3MF per-filament slicer data (primary path).

    Uses slicer-estimated filament weight for all spools (BL and non-BL).
    For partial prints (failed/aborted), tries per-layer gcode data first,
    then falls back to linear scaling by progress.

    Slot-to-tray mapping priority (both dispatched sources before the live one —
    AMS filament backup rewrites the live MQTT field to the substitute tray):
    1. Stored ams_mapping from print command (reprints/direct prints)
    2. Queue item ams_mapping (for queue-initiated prints)
    3. MQTT mapping field from printer state (universal, all print sources)
    4. tray_now from printer state (for single-filament non-queue prints)
    5. Position-based default using sorted available tray IDs (handles external spools)
    6. Default mapping: slot_id - 1 = global_tray_id (last resort)
    """
    from backend.app.core.config import settings as app_settings
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.utils.threemf_tools import extract_filament_usage_from_3mf

    result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    archive = result.scalar_one_or_none()
    if not archive or not archive.file_path:
        logger.info("[UsageTracker] 3MF: archive %s has no file_path, skipping", archive_id)
        return []

    file_path = app_settings.base_dir / archive.file_path
    if not file_path.exists():
        logger.info("[UsageTracker] 3MF: file not found: %s", file_path)
        return []

    # Scope the extract to the dispatched plate (#1697). ``archive.plate_index``
    # is our authoritative "which plate ran" record — set by the dispatcher for
    # queue and direct prints alike, in the same 1-based convention the parser
    # expects. Without it a single-plate job from a multi-plate 3MF debits the
    # spool for every plate's filament. None (external/screen prints where we
    # can't know the plate) → whole-file sum, unchanged.
    filament_usage = extract_filament_usage_from_3mf(file_path, archive.plate_index)
    if not filament_usage:
        logger.info("[UsageTracker] 3MF: no filament usage data in %s", file_path)
        return []

    logger.info("[UsageTracker] 3MF: archive %s, filament_usage=%s", archive_id, filament_usage)

    # --- Resolve slot-to-tray mapping ---
    mapping_source = None

    # 1. Use stored ams_mapping from the print command (reprints/direct prints)
    slot_to_tray = ams_mapping
    if slot_to_tray:
        mapping_source = "print_cmd"

    # 2. Try queue item ams_mapping (queue-initiated prints store the exact mapping)
    #
    # ⚠️ Ranked ABOVE the live MQTT field on purpose: ``mapping`` reports the
    # tray the printer is feeding from *now*, and AMS filament backup rewrites
    # it to the substitute tray when a spool runs dry. Read at completion it
    # names the tray that finished the print, not the one the slicer assigned —
    # so the spool that actually emptied was charged nothing and the backup
    # spool was charged the lot. The queue item's copy is the mapping the print
    # was dispatched with, and it is in the database, so it also survives a
    # restart mid-print.
    if not slot_to_tray and archive_id:
        queue_result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.archive_id == archive_id)
            .where(PrintQueueItem.status.in_(["printing", "completed", "failed"]))
        )
        # ``.first()``, not ``.scalar_one_or_none()``: a re-print points a second
        # queue item at the same archive, and raising MultipleResultsFound here
        # would cost the print all of its usage tracking.
        queue_item = queue_result.scalars().first()
        if queue_item and queue_item.ams_mapping:
            try:
                slot_to_tray = json.loads(queue_item.ams_mapping)
                mapping_source = "queue"
            except (json.JSONDecodeError, TypeError):
                pass

    # 3. Try MQTT mapping field from printer state (universal, all print sources)
    if not slot_to_tray:
        state = printer_manager.get_status(printer_id)
        raw_data = getattr(state, "raw_data", None) if state else None
        if raw_data:
            mqtt_mapping = raw_data.get("mapping")
            decoded = _decode_mqtt_mapping(mqtt_mapping)
            if decoded:
                slot_to_tray = decoded
                mapping_source = "mqtt"

    # 4. Color-match 3MF filament slots to AMS trays (for printers without mapping field)
    if not slot_to_tray:
        state = printer_manager.get_status(printer_id)
        raw_data = getattr(state, "raw_data", None) if state else None
        if raw_data:
            matched = _match_slots_by_color(filament_usage, raw_data.get("ams"))
            if matched:
                slot_to_tray = matched
                mapping_source = "color_match"

    logger.info(
        "[UsageTracker] 3MF: slot_to_tray=%s (source: %s)",
        slot_to_tray,
        mapping_source or "none",
    )

    # 5. For single-filament non-queue prints, use tray_now from printer state
    #    Priority: tray_change_log (multi-tray split) > tray_now_at_start > current tray_now
    #              > last_loaded_tray > vt_tray check
    #
    # tray_change_log evidence wins over slot_to_tray when present: if the
    # printer fed from multiple trays mid-print (AMS auto-fallback when one
    # spool runs out, #957), the slicer's mapping captured at print start is
    # stale and must be replaced with per-layer split attribution. The pre-fix
    # gate ``not slot_to_tray and len(nonzero_slots) == 1`` only allowed the
    # splitter to run when the slicer mapping had been lost — so the actual
    # fallback case (slot_to_tray populated by every print_cmd) silently
    # double-credited.
    nonzero_slots = [u for u in filament_usage if u.get("used_g", 0) > 0]
    tray_now_override: int | None = None
    tray_changes: list[tuple[int, int]] = []  # [(global_tray_id, layer_num), ...]
    state = printer_manager.get_status(printer_id) if len(nonzero_slots) == 1 else None
    if state is not None:
        tray_changes = getattr(state, "tray_change_log", []) or []
    elif len(nonzero_slots) > 1:
        # Multi-material print: every filament change moves tray_now, so the log
        # can't be read as "this slot moved to that tray" and splitting would
        # attribute worse than the mapping does. Say so rather than silently
        # dropping the evidence — a runout mid-print on a multi-material job
        # still lands entirely on the mapped tray.
        _multi_state = printer_manager.get_status(printer_id)
        if len(getattr(_multi_state, "tray_change_log", []) or []) > 1:
            logger.warning(
                "[UsageTracker] 3MF: %d tray changes observed but %d filament slots used — "
                "splitting needs a single slot, attributing by mapping alone (printer %d, archive %s)",
                len(_multi_state.tray_change_log),
                len(nonzero_slots),
                printer_id,
                archive_id,
            )

    if len(tray_changes) > 1:
        # Multi-tray usage detected — splitting takes over regardless of
        # slot_to_tray. Path 2 (AMS remain%-delta fallback) then naturally
        # skips both trays because they're already in handled_trays after
        # splitting, eliminating the double-credit.
        logger.info("[UsageTracker] 3MF: tray change log: %s (will split weight)", tray_changes)
    elif not slot_to_tray and len(nonzero_slots) == 1:
        if 0 <= tray_now_at_start <= 254:
            # Try tray_now_at_start first (captured at print start)
            tray_now_override = tray_now_at_start
            logger.info("[UsageTracker] 3MF: using tray_now_at_start=%d (single-filament fallback)", tray_now_at_start)
        elif state and 0 <= state.tray_now <= 254:
            # Current state is valid (printer didn't retract yet)
            tray_now_override = state.tray_now
            logger.info("[UsageTracker] 3MF: using current tray_now=%d", state.tray_now)
        elif state and 0 <= state.last_loaded_tray <= 253:
            # Last valid tray before retract (H2D retracts before completion callback)
            tray_now_override = state.last_loaded_tray
            logger.info("[UsageTracker] 3MF: using last_loaded_tray=%d (post-retract fallback)", state.last_loaded_tray)
        elif state and state.tray_now == 255:
            # 255 = "no filament" on legacy printers, but valid 2nd external spool on H2-series
            vt_tray = state.raw_data.get("vt_tray") or []
            if any(int(vt.get("id", 0)) == 255 for vt in vt_tray if isinstance(vt, dict)):
                tray_now_override = state.tray_now
                logger.info("[UsageTracker] 3MF: using tray_now=255 (H2-series external spool)")
        if tray_now_override is None:
            logger.info(
                "[UsageTracker] 3MF: no valid tray_now (at_start=%d, current=%s, last_loaded=%s)",
                tray_now_at_start,
                state.tray_now if state else "N/A",
                state.last_loaded_tray if state else "N/A",
            )

    # Scale factor for partial prints (failed/aborted)
    if status == "completed":
        scale = 1.0
    else:
        state = printer_manager.get_status(printer_id)
        progress = state.progress if state else 0
        # Firmware resets progress to 0 on cancel - use last valid progress captured during print
        if progress <= 0 and last_progress > 0:
            progress = last_progress
            logger.info("[UsageTracker] 3MF: using last_progress=%.1f (firmware reset current to 0)", last_progress)
        scale = max(0.0, min(progress / 100.0, 1.0))

    # Per-layer gcode accuracy for partial prints
    layer_grams: dict[int, float] | None = None
    if status != "completed":
        state = printer_manager.get_status(printer_id)
        current_layer = state.layer_num if state else 0
        # Firmware resets layer_num to 0 on cancel - use last valid layer captured during print
        if current_layer <= 0 and last_layer_num > 0:
            current_layer = last_layer_num
            logger.info("[UsageTracker] 3MF: using last_layer_num=%d (firmware reset current to 0)", last_layer_num)
        if current_layer > 0:
            try:
                from backend.app.utils.threemf_tools import (
                    extract_filament_properties_from_3mf,
                    extract_layer_filament_usage_from_3mf,
                    get_cumulative_usage_at_layer,
                    mm_to_grams,
                )

                layer_usage = extract_layer_filament_usage_from_3mf(file_path, archive.plate_index)
                if layer_usage:
                    cumulative_mm = get_cumulative_usage_at_layer(layer_usage, current_layer)
                    filament_props = extract_filament_properties_from_3mf(file_path)
                    layer_grams = {}
                    for filament_id, mm_used in cumulative_mm.items():
                        slot_id = filament_id + 1  # 0-based to 1-based
                        props = filament_props.get(slot_id, {})
                        density = props.get("density", 1.24)
                        diameter = props.get("diameter", 1.75)
                        layer_grams[slot_id] = mm_to_grams(mm_used, diameter, density)
            except Exception:
                pass  # Fall back to linear scaling

    results = []

    for usage in filament_usage:
        slot_id = usage.get("slot_id", 0)
        used_g = usage.get("used_g", 0)
        if used_g <= 0:
            continue

        # --- Mid-print tray switch: split weight across trays ---
        # Split math is shared with the Spoolman writer via
        # ``utils.tray_split.compute_tray_split_grams`` (#1793) — both
        # inventory backends must attribute segments identically or a user
        # running dual-mode sees divergent totals.
        if len(tray_changes) > 1:
            # Compute total weight for this slot (same logic as normal path)
            if layer_grams and slot_id in layer_grams:
                total_weight = layer_grams[slot_id]
            else:
                total_weight = used_g * scale

            if total_weight <= 0:
                continue

            # Extract per-layer gcode for segment splitting
            split_layer_usage = None
            split_props: dict = {}
            try:
                from backend.app.utils.threemf_tools import (
                    extract_filament_properties_from_3mf,
                    extract_layer_filament_usage_from_3mf,
                )

                split_layer_usage = extract_layer_filament_usage_from_3mf(file_path, archive.plate_index)
                filament_props = extract_filament_properties_from_3mf(file_path)
                split_props = filament_props.get(slot_id, {})
            except Exception:
                pass  # Fall back to linear splitting

            from backend.app.utils.tray_split import compute_tray_split_grams

            segments = compute_tray_split_grams(
                tray_changes=tray_changes,
                total_weight=total_weight,
                slot_id=slot_id,
                layer_usage=split_layer_usage,
                density=split_props.get("density", 1.24),
                diameter=split_props.get("diameter", 1.75),
                total_layers=(state.total_layers if state else 0) or 0,
                last_layer_num=last_layer_num,
            )
            segments = _add_autoswitch_purge(segments, tray_changes, journal_events or [], runout_purge_grams)

            for seg_idx, tray_global, segment_grams in segments:
                if segment_grams <= 0:
                    continue

                # Convert global tray ID to (ams_id, tray_id)
                if tray_global >= 254:
                    seg_ams_id = 255
                    seg_tray_id = tray_global - 254
                elif tray_global >= 128:
                    seg_ams_id = tray_global
                    seg_tray_id = 0
                else:
                    seg_ams_id = tray_global // 4
                    seg_tray_id = tray_global % 4

                seg_key = (seg_ams_id, seg_tray_id)
                if seg_key in handled_trays:
                    continue

                seg_start_layer = tray_changes[seg_idx][1]
                is_last = seg_idx + 1 >= len(tray_changes)
                logger.info(
                    "[UsageTracker] 3MF split: segment %d tray=%d (AMS%d-T%d) layers %d-%s -> %.1fg",
                    seg_idx,
                    tray_global,
                    seg_ams_id,
                    seg_tray_id,
                    seg_start_layer,
                    tray_changes[seg_idx + 1][1] if not is_last else "end",
                    segment_grams,
                )

                seg_spool_id = await _resolve_spool_id_for_tray(
                    printer_id=printer_id,
                    ams_id=seg_ams_id,
                    tray_id=seg_tray_id,
                    db=db,
                    spool_assignments_snapshot=spool_assignments,
                    print_started_at=print_started_at,
                )
                if seg_spool_id is None:
                    logger.info(
                        "[UsageTracker] 3MF split: no spool at printer %d AMS%d-T%d, skipping segment",
                        printer_id,
                        seg_ams_id,
                        seg_tray_id,
                    )
                    continue

                spool_result = await db.execute(select(Spool).where(Spool.id == seg_spool_id))
                spool = spool_result.scalar_one_or_none()
                if not spool:
                    continue

                spool.weight_used = (spool.weight_used or 0) + segment_grams
                spool.last_used = datetime.now(timezone.utc)
                await _warn_if_low_stock(db, spool, printer_id, tray_global)

                percent = round(segment_grams / (spool.label_weight or 1000) * 100)

                cost = None
                cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
                if cost_per_kg > 0:
                    cost = round((segment_grams / 1000.0) * cost_per_kg, 2)

                history = SpoolUsageHistory(
                    spool_id=spool.id,
                    printer_id=printer_id,
                    print_name=print_name,
                    weight_used=round(segment_grams, 1),
                    percent_used=percent,
                    status=status,
                    cost=cost,
                    archive_id=archive_id,
                )
                db.add(history)

                handled_trays.add(seg_key)
                results.append(
                    {
                        "spool_id": spool.id,
                        "weight_used": round(segment_grams, 1),
                        "percent_used": percent,
                        "ams_id": seg_ams_id,
                        "tray_id": seg_tray_id,
                        "material": spool.material,
                        "cost": cost,
                        "slot_id": slot_id,
                        "color": _spool_color_to_hex(spool.rgba),
                    }
                )

                logger.info(
                    "[UsageTracker] Spool %d consumed %.1fg (3MF split seg%d) on printer %d AMS%d-T%d (%s)",
                    spool.id,
                    segment_grams,
                    seg_idx,
                    printer_id,
                    seg_ams_id,
                    seg_tray_id,
                    status,
                )

            continue  # Skip normal single-tray processing for this slot

        # Map 3MF slot_id to physical (ams_id, tray_id) using resolved mapping
        if tray_now_override is not None:
            # Single-filament non-queue print: use actual tray from printer state
            global_tray_id = tray_now_override
        else:
            # Explicit mapping (print command, MQTT, queue, color match)
            global_tray_id = None
            if slot_to_tray and slot_id <= len(slot_to_tray):
                mapped = slot_to_tray[slot_id - 1]
                if isinstance(mapped, int) and mapped >= 0:
                    global_tray_id = mapped
            # Position-based default: sort available tray IDs so external spools (254/255)
            # naturally follow standard AMS trays, matching slicer slot numbering
            if global_tray_id is None:
                _state = printer_manager.get_status(printer_id)
                _raw = getattr(_state, "raw_data", None) if _state else None
                if _raw:
                    from backend.app.services.spoolman_tracking import build_ams_tray_lookup

                    # Filter out AMS slots with no spool loaded (empty tray_type):
                    # BambuStudio/OrcaSlicer compact the slot list when assigning
                    # filaments and don't expose empty AMS slots, so the slicer's
                    # 3MF slot N maps to the Nth *loaded* tray, not the Nth physical
                    # position. Without this, a "3 AMS loaded + 1 empty + external"
                    # layout routed the slicer's 4th filament to the empty AMS slot
                    # instead of the external, and the external's usage was never
                    # recorded (#1607). vt_tray entries are already filtered this
                    # way inside build_ams_tray_lookup — mirror it for AMS here.
                    _lookup = build_ams_tray_lookup(_raw)
                    available_trays = sorted(gid for gid, info in _lookup.items() if info.get("tray_type"))
                    if slot_id <= len(available_trays):
                        global_tray_id = available_trays[slot_id - 1]
            # Final fallback: slot_id - 1 (legacy, works for pure AMS without external spools)
            if global_tray_id is None:
                global_tray_id = slot_id - 1

        if global_tray_id >= 254:
            # External spool: ams_id=255 (sentinel), tray_id=slot index (0 or 1)
            ams_id = 255
            tray_id = global_tray_id - 254
        elif global_tray_id >= 128:
            ams_id = global_tray_id
            tray_id = 0
        else:
            ams_id = global_tray_id // 4
            tray_id = global_tray_id % 4

        logger.info(
            "[UsageTracker] 3MF: slot_id=%d -> global_tray=%d -> AMS%d-T%d (used_g=%.1f, tray_now_override=%s)",
            slot_id,
            global_tray_id,
            ams_id,
            tray_id,
            used_g,
            tray_now_override,
        )

        key = (ams_id, tray_id)
        if key in handled_trays:
            continue

        # --- Journal-driven split: a runout on this slot's tray ---
        # Spool-change boundaries with ids frozen at event time. Takes over
        # only when the tray-change split above didn't (that one owns
        # single-slot multi-tray prints); the boundary math is the same
        # helper, so the two cannot drift.
        journal_segs = (
            journal_boundaries_for_tray(journal_events, global_tray_id)
            if journal_events and len(tray_changes) <= 1
            else []
        )
        if len(journal_segs) > 1:
            if layer_grams and slot_id in layer_grams:
                total_weight = layer_grams[slot_id]
            else:
                total_weight = used_g * scale
            if total_weight <= 0:
                continue

            seg_layer_usage = None
            seg_props: dict = {}
            try:
                from backend.app.utils.threemf_tools import (
                    extract_filament_properties_from_3mf,
                    extract_layer_filament_usage_from_3mf,
                )

                seg_layer_usage = extract_layer_filament_usage_from_3mf(file_path, archive.plate_index)
                seg_props = extract_filament_properties_from_3mf(file_path).get(slot_id, {})
            except Exception:
                pass  # linear fallback inside the helper

            from backend.app.utils.tray_split import compute_layer_segment_grams

            state = printer_manager.get_status(printer_id)
            seg_grams = compute_layer_segment_grams(
                boundary_layers=[start for start, _, _ in journal_segs],
                total_weight=total_weight,
                slot_id=slot_id,
                layer_usage=seg_layer_usage,
                density=seg_props.get("density", 1.24),
                diameter=seg_props.get("diameter", 1.75),
                total_layers=(state.total_layers if state else 0) or 0,
                last_layer_num=last_layer_num,
            )
            _purge = _autoswitch_purge_for_tray(journal_events or [], global_tray_id, runout_purge_grams)
            if _purge > 0 and len(seg_grams) > 1:
                seg_grams[1] += _purge

            for (seg_start, seg_spool_id, _), segment_grams in zip(journal_segs, seg_grams, strict=True):
                if segment_grams <= 0:
                    continue
                if seg_spool_id is None:
                    logger.info(
                        "[UsageTracker] 3MF journal split: segment from layer %d has no attributable spool "
                        "(printer %d tray %d) — %.1fg uncharged rather than guessed",
                        seg_start,
                        printer_id,
                        global_tray_id,
                        segment_grams,
                    )
                    continue
                spool_result = await db.execute(select(Spool).where(Spool.id == seg_spool_id))
                spool = spool_result.scalar_one_or_none()
                if not spool:
                    continue

                spool.weight_used = (spool.weight_used or 0) + segment_grams
                spool.last_used = datetime.now(timezone.utc)
                await _warn_if_low_stock(db, spool, printer_id, global_tray_id)

                percent = round(segment_grams / (spool.label_weight or 1000) * 100)
                cost = None
                cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
                if cost_per_kg > 0:
                    cost = round((segment_grams / 1000.0) * cost_per_kg, 2)

                db.add(
                    SpoolUsageHistory(
                        spool_id=spool.id,
                        printer_id=printer_id,
                        print_name=print_name,
                        weight_used=round(segment_grams, 1),
                        percent_used=percent,
                        status=status,
                        cost=cost,
                        archive_id=archive_id,
                    )
                )
                results.append(
                    {
                        "spool_id": spool.id,
                        "weight_used": round(segment_grams, 1),
                        "percent_used": percent,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                        "material": spool.material,
                        "cost": cost,
                        "slot_id": slot_id,
                        "color": _spool_color_to_hex(spool.rgba),
                    }
                )
                logger.info(
                    "[UsageTracker] Spool %d consumed %.1fg (journal split from layer %d) on printer %d tray %d (%s)",
                    spool.id,
                    segment_grams,
                    seg_start,
                    printer_id,
                    global_tray_id,
                    status,
                )

            handled_trays.add(key)
            continue

        spool_id = await _resolve_spool_id_for_tray(
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            db=db,
            spool_assignments_snapshot=spool_assignments,
            print_started_at=print_started_at,
        )
        if spool_id is None:
            logger.info("[UsageTracker] 3MF: no spool assignment at printer %d AMS%d-T%d", printer_id, ams_id, tray_id)
            continue

        # Load spool
        spool_result = await db.execute(select(Spool).where(Spool.id == spool_id))
        spool = spool_result.scalar_one_or_none()
        if not spool:
            continue

        # Use per-layer grams if available, otherwise linear scale
        if layer_grams and slot_id in layer_grams:
            weight_grams = layer_grams[slot_id]
        else:
            weight_grams = used_g * scale

        if weight_grams <= 0:
            continue

        # Update spool
        spool.weight_used = (spool.weight_used or 0) + weight_grams
        spool.last_used = datetime.now(timezone.utc)
        await _warn_if_low_stock(db, spool, printer_id, global_tray_id)

        percent = round(weight_grams / (spool.label_weight or 1000) * 100)

        # Calculate cost for this usage
        cost = None
        cost_per_kg = spool.cost_per_kg if spool.cost_per_kg is not None else default_filament_cost
        if cost_per_kg > 0:
            cost = round((weight_grams / 1000.0) * cost_per_kg, 2)

        # Insert usage history record
        history = SpoolUsageHistory(
            spool_id=spool.id,
            printer_id=printer_id,
            print_name=print_name,
            weight_used=round(weight_grams, 1),
            percent_used=percent,
            status=status,
            cost=cost,
            archive_id=archive_id,
        )
        db.add(history)

        handled_trays.add(key)
        results.append(
            {
                "spool_id": spool.id,
                "weight_used": round(weight_grams, 1),
                "percent_used": percent,
                "ams_id": ams_id,
                "tray_id": tray_id,
                "material": spool.material,
                "cost": cost,
                "slot_id": slot_id,
                "color": _spool_color_to_hex(spool.rgba),
            }
        )

        # Determine mapping source for debug logging
        if tray_now_override is not None:
            map_src = ", tray_now"
        elif mapping_source:
            map_src = f", {mapping_source}_map"
        else:
            map_src = ""
        logger.info(
            "[UsageTracker] Spool %d consumed %.1fg (3MF%s%s) on printer %d AMS%d-T%d (%s)",
            spool.id,
            weight_grams,
            " per-layer" if (layer_grams and slot_id in layer_grams) else (f" scaled {scale:.0%}" if scale < 1 else ""),
            map_src,
            printer_id,
            ams_id,
            tray_id,
            status,
        )

    # --- Adopt the matched inventory spools' colours for the archive (#1494) ---
    # The archive's filament_color was set from the slicer's 3MF at creation
    # time; now that every used slot has been resolved to an inventory spool,
    # the curated spool colour is authoritative. Committed by the caller.
    if archive is not None:
        spool_colors = _archive_colors_from_spools(filament_usage, results)
        if spool_colors:
            joined = ",".join(spool_colors)
            if joined != archive.filament_color:
                logger.info(
                    "[UsageTracker] 3MF: archive %s filament_color %r -> %r (from inventory spools)",
                    archive_id,
                    archive.filament_color,
                    joined,
                )
                archive.filament_color = joined

        # --- Adopt the matched inventory spools' materials too (#2563) ---
        # A slot hand-mapped in the Print dialog to a differently-typed loaded
        # spool than it was sliced for (a PLA slice routed to the only loaded PETG
        # slot) otherwise stays filed under the sliced material in the archive
        # card, the material filter and the Statistics material graphs — even
        # though the correct spool was debited. Same reasoning as the colour
        # adoption above. Note the separator: ``archive.py`` writes filament_type
        # comma-SPACE joined (filament_color is comma-only) and the frontend
        # material graphs split on ', ', so a bare comma would collapse
        # "PLA, PETG" into one bogus material bucket. Committed by the caller.
        spool_types = _archive_types_from_spools(filament_usage, results)
        if spool_types:
            joined_types = ", ".join(spool_types)
            if joined_types != archive.filament_type:
                logger.info(
                    "[UsageTracker] 3MF: archive %s filament_type %r -> %r (from inventory spools)",
                    archive_id,
                    archive.filament_type,
                    joined_types,
                )
                archive.filament_type = joined_types

    return results
