"""Writer/reader for the append-only per-print usage journal.

The journal (``print_usage_events``, m153) is the persisted record of which
spool fed which layers: tray changes, runouts, replacement loads, pause/resume.
Spool identity is FROZEN at event time via :func:`freeze_spool_ids` — both
inventory backends in the same row — so the completion path attributes
segments without re-asking the DB after the fact.

Retention: :func:`prune_finished` runs in main.py's periodic cleanup loop
(``usage_events_retention_hours`` setting); :func:`delete_for_archive` covers
the archive hard-delete path, because the FK CASCADE on the table fires on
PostgreSQL only — this codebase never sets ``PRAGMA foreign_keys``.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_usage_event import PrintUsageEvent

logger = logging.getLogger(__name__)


def _global_tray_to_ams_slot(global_tray_id: int) -> tuple[int, int]:
    """Decode a global tray id to the ``(ams_id, tray_id)`` the assignment
    tables are keyed by — same encoding as ``spoolman_tracking``:
    254/255 → external (ams 255, tray 0/1), >=128 → AMS-HT (own id, tray 0),
    else a four-slot AMS unit."""
    if global_tray_id >= 254:
        return 255, global_tray_id - 254
    if global_tray_id >= 128:
        return global_tray_id, 0
    return global_tray_id // 4, global_tray_id % 4


async def record_event(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int,
    layer_num: int,
    event: str,
    kind: str | None = None,
    global_tray_id: int | None = None,
    spool_id: int | None = None,
    spoolman_spool_id: int | None = None,
) -> None:
    """Append one journal row and commit."""
    db.add(
        PrintUsageEvent(
            printer_id=printer_id,
            archive_id=archive_id,
            layer_num=layer_num,
            event=event,
            kind=kind,
            global_tray_id=global_tray_id,
            spool_id=spool_id,
            spoolman_spool_id=spoolman_spool_id,
        )
    )
    await db.commit()


async def freeze_spool_ids(db: AsyncSession, printer_id: int, global_tray_id: int) -> tuple[int | None, int | None]:
    """The spools currently bound to a tray, for both backends — or ``None``s.

    Called at event time so the journal row carries the answer the completion
    path needs; an unassigned slot freezes to nothing rather than guessing.
    """
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    ams_id, tray_id = _global_tray_to_ams_slot(global_tray_id)

    spool_id = (
        await db.execute(
            select(SpoolAssignment.spool_id).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()

    spoolman_spool_id = (
        await db.execute(
            select(SpoolmanSlotAssignment.spoolman_spool_id).where(
                SpoolmanSlotAssignment.printer_id == printer_id,
                SpoolmanSlotAssignment.ams_id == ams_id,
                SpoolmanSlotAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()

    return spool_id, spoolman_spool_id


async def active_archive_id(db: AsyncSession, printer_id: int) -> int | None:
    """The printer's newest still-printing archive — the journal's anchor.

    The archive row exists from print start (external prints included), so a
    tracked print always has one; ``None`` means no tracked print is running
    and the event has nothing to attach to.
    """
    from backend.app.models.archive import PrintArchive

    result = await db.execute(
        select(PrintArchive.id)
        .where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
        .order_by(PrintArchive.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def record_runout(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int,
    layer_num: int,
    kind: str | None,
    global_tray_id: int | None,
    spool_id: int | None = None,
    spoolman_spool_id: int | None = None,
) -> None:
    """One runout row per EPISODE of (archive, tray).

    A repeat while the episode is open (the detector re-fired on HMS flicker,
    or a restart replayed the still-active code) only upgrades the kind — the
    boundary layer and the frozen spool ids of the first sighting stay. But a
    runout whose previous episode is CLOSED (a spool_loaded followed it) is a
    new episode — two short reels in one long print — and gets its own row.
    """
    from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_SPOOL_LOADED

    rows = (
        (
            await db.execute(
                select(PrintUsageEvent)
                .where(
                    PrintUsageEvent.archive_id == archive_id,
                    PrintUsageEvent.event.in_([EVENT_RUNOUT, EVENT_SPOOL_LOADED]),
                    PrintUsageEvent.global_tray_id == global_tray_id,
                )
                .order_by(PrintUsageEvent.id)
            )
        )
        .scalars()
        .all()
    )
    last_runout = next((e for e in reversed(rows) if e.event == EVENT_RUNOUT), None)
    if last_runout is not None:
        closed = any(e.event == EVENT_SPOOL_LOADED and e.id > last_runout.id for e in rows)
        if not closed:
            if kind and last_runout.kind != kind:
                last_runout.kind = kind
                await db.commit()
            return

    await record_event(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_RUNOUT,
        kind=kind,
        global_tray_id=global_tray_id,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )


async def note_assignment_change(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    spool_id: int | None = None,
    spoolman_spool_id: int | None = None,
    layer_num: int | None = None,
) -> None:
    """A slot got a (re)assigned spool — if it ran out during the active
    print, this IS the replacement: journal EVENT_SPOOL_LOADED.

    The RFID uuid-watch covers tagged spools; this covers the tagless ones
    (external holders, untagged reels), where "assign the replacement in
    BamDude" — exactly what the runout notification asks for — is the only
    signal there is. No runout on the tray, a replacement already noted, or
    re-linking the very reel the runout froze → no-op.
    """
    from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_SPOOL_LOADED

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return

    if ams_id >= 254:
        global_tray = 254 + tray_id
    elif ams_id >= 128:
        global_tray = ams_id
    else:
        global_tray = ams_id * 4 + tray_id

    events = await load_events(db, printer_id, archive_id)
    # The LAST runout on the tray — episodes are closed in order, and a second
    # runout of the same tray (two short reels) needs its own replacement.
    runout = next((e for e in reversed(events) if e.event == EVENT_RUNOUT and e.global_tray_id == global_tray), None)
    if runout is None:
        return
    already = next(
        (e for e in events if e.id > runout.id and e.event == EVENT_SPOOL_LOADED and e.global_tray_id == global_tray),
        None,
    )
    if already is not None:
        # RFID-refill race: the uuid-watch fires on the first push carrying the
        # new tag and freezes whatever assignment existed at that instant —
        # which can still be the OLD spool (auto-assign commits moments later)
        # or nothing. The late assignment is the authoritative answer, so it
        # CORRECTS the row rather than being skipped; identical ids are a no-op.
        changed = False
        if spool_id is not None and already.spool_id != spool_id:
            already.spool_id = spool_id
            changed = True
        if spoolman_spool_id is not None and already.spoolman_spool_id != spoolman_spool_id:
            already.spoolman_spool_id = spoolman_spool_id
            changed = True
        if changed:
            logger.info(
                "[UsageJournal] Corrected spool_loaded for tray %d on printer %d (spool=%s, spoolman=%s)",
                global_tray,
                printer_id,
                spool_id,
                spoolman_spool_id,
            )
            await db.commit()
        return
    if spool_id is not None and runout.spool_id == spool_id:
        return  # the same reel re-linked — a correction, not a replacement
    if spool_id is None and spoolman_spool_id is not None and runout.spoolman_spool_id == spoolman_spool_id:
        return

    if layer_num is None:
        try:
            from backend.app.services.printer_manager import printer_manager

            state = printer_manager.get_status(printer_id)
            layer_num = getattr(state, "layer_num", 0) or 0
        except Exception:
            layer_num = 0

    logger.info(
        "[UsageJournal] Replacement assigned for tray %d on printer %d (spool=%s, spoolman=%s) at layer %d",
        global_tray,
        printer_id,
        spool_id,
        spoolman_spool_id,
        layer_num,
    )
    await record_event(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_SPOOL_LOADED,
        global_tray_id=global_tray,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )


async def load_events(db: AsyncSession, printer_id: int, archive_id: int) -> list[PrintUsageEvent]:
    """The print's journal, in insertion order."""
    result = await db.execute(
        select(PrintUsageEvent)
        .where(
            PrintUsageEvent.printer_id == printer_id,
            PrintUsageEvent.archive_id == archive_id,
        )
        .order_by(PrintUsageEvent.id)
    )
    return list(result.scalars().all())


async def prune_finished(db: AsyncSession, retention_hours: int) -> int:
    """Delete journal rows of finished prints older than the retention window.

    Rows of a still-``printing`` archive are never touched, whatever their age
    — an active print's journal is its accounting record, not history yet.
    Returns the number of rows deleted.
    """
    from backend.app.models.archive import PrintArchive

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=retention_hours)
    result = await db.execute(
        delete(PrintUsageEvent).where(
            PrintUsageEvent.created_at < cutoff,
            PrintUsageEvent.archive_id.in_(select(PrintArchive.id).where(PrintArchive.status != "printing")),
        )
    )
    await db.commit()
    deleted = result.rowcount or 0
    if deleted:
        logger.info("[UsageJournal] Pruned %d journal row(s) older than %dh", deleted, retention_hours)
    return deleted


async def delete_for_archive(db: AsyncSession, archive_id: int) -> None:
    """Drop an archive's journal rows (SQLite honours no FK cascade)."""
    await db.execute(delete(PrintUsageEvent).where(PrintUsageEvent.archive_id == archive_id))
