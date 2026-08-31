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

    Two hardenings from the X2D incident (2026-08-23):

    * A runout the resolver could NOT map to a tray is folded away when a
      tray-ful episode is already open — the printer fires several codes for
      one physical event (a per-slot HMS plus a generic print_error 24s
      apart), and the tray-less one adds nothing the timeline doesn't have.
    * A runout arriving with no frozen spool inherits it from the journal's
      own prior rows on the same tray. By runout time the slot's assignment
      can already be gone (the AMS-empty report unlinked it, or the user
      unassigned by hand) — but the journal recorded who fed the tray, and
      inheriting recorded lineage is not guessing.
    """
    from backend.app.models.print_usage_event import (
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        EVENT_START,
        EVENT_TRAY_CHANGE,
    )

    if global_tray_id is None:
        tray_rows = (
            (
                await db.execute(
                    select(PrintUsageEvent)
                    .where(
                        PrintUsageEvent.archive_id == archive_id,
                        PrintUsageEvent.event.in_([EVENT_RUNOUT, EVENT_SPOOL_LOADED]),
                        PrintUsageEvent.global_tray_id.is_not(None),
                    )
                    .order_by(PrintUsageEvent.id)
                )
            )
            .scalars()
            .all()
        )
        open_trays = set()
        for e in tray_rows:
            if e.event == EVENT_RUNOUT:
                open_trays.add(e.global_tray_id)
            else:
                open_trays.discard(e.global_tray_id)
        if open_trays:
            logger.debug(
                "Tray-less runout folded into open episode on tray(s) %s (archive %d)",
                sorted(open_trays),
                archive_id,
            )
            return

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

    if spool_id is None and spoolman_spool_id is None and global_tray_id is not None:
        lineage = (
            await db.execute(
                select(PrintUsageEvent)
                .where(
                    PrintUsageEvent.archive_id == archive_id,
                    PrintUsageEvent.global_tray_id == global_tray_id,
                    PrintUsageEvent.event.in_([EVENT_START, EVENT_TRAY_CHANGE, EVENT_SPOOL_LOADED]),
                    (PrintUsageEvent.spool_id.is_not(None)) | (PrintUsageEvent.spoolman_spool_id.is_not(None)),
                )
                .order_by(PrintUsageEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if lineage is not None:
            spool_id = lineage.spool_id
            spoolman_spool_id = lineage.spoolman_spool_id
            logger.info(
                "Runout on tray %s had no live assignment — inherited spool %s/%s from the journal's own lineage",
                global_tray_id,
                spool_id,
                spoolman_spool_id,
            )

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


async def _slot_holds_a_spool(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Is there a spool bound to this slot that an assignment would replace?

    ⚠️ **The books, not the tray.** A reel that runs out mid-print leaves the
    tray reporting empty while ``on_ams_change`` deliberately keeps the slot
    linked — "the spool is still physically in the AMS, just consumed", and that
    link is the only record of what fed the print. Reading emptiness off the AMS
    would call a runout an empty slot, which is the one case the replacement
    question exists for.
    """
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    for model in (SpoolAssignment, SpoolmanSlotAssignment):
        bound = await db.execute(
            select(model.id).where(
                model.printer_id == printer_id,
                model.ams_id == ams_id,
                model.tray_id == tray_id,
            )
        )
        if bound.scalar_one_or_none() is not None:
            return True
    return False


async def manual_replacement_window(
    db: AsyncSession,
    printer_id: int,
    *,
    ams_id: int | None = None,
    tray_id: int | None = None,
) -> dict | None:
    """Is a declared mid-print replacement currently plausible, and how?

    ⚠️ **A replacement is a property of the SLOT, not only of the print.**
    Declaring one charges everything printed so far to the spool that came OUT,
    and an empty slot has none — ``freeze_spool_ids`` freezes it to nothing
    rather than guessing, so the branch would journal a boundary naming nobody.
    Filling an empty slot mid-print is therefore never a replacement, and asking
    about it puts an unanswerable question in front of the operator. When
    ``ams_id``/``tray_id`` are given, an unbound slot answers ``None``; callers
    that ask nothing about a slot keep the printer-wide answer.

    The feeding spool can only be swapped while the print is paused, so the
    window is defined by pauses, not by the assignment's moment:

    * printer PAUSED right now → ``{"mode": "prompt", "pause_layer": current}``
      — a swap is likely, the UI asks a blocking question;
    * RUNNING but this print HAS a pause behind it → ``{"mode": "optin",
      "pause_layer": last-pause layer}`` — the swap, if any, happened back at
      that pause (swap → resume from the printer's screen → only then the UI),
      so the UI offers a default-off checkbox and the boundary lands on the
      pause layer, not on the click;
    * no active print, or never paused → ``None`` — a replacement is
      physically impossible, every assignment is a wrong-link correction and
      must stay friction-free (bulk re-linking mid-print is a real workflow).
    """
    from backend.app.models.print_usage_event import EVENT_PAUSE

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return None
    if ams_id is not None and tray_id is not None:
        if not await _slot_holds_a_spool(db, printer_id, ams_id, tray_id):
            return None
    try:
        from backend.app.services.printer_manager import printer_manager

        state = printer_manager.get_status(printer_id)
    except Exception:
        state = None
    if state is not None and (getattr(state, "state", None) or "").upper() == "PAUSE":
        return {"archive_id": archive_id, "mode": "prompt", "pause_layer": getattr(state, "layer_num", 0) or 0}
    last_pause = (
        await db.execute(
            select(PrintUsageEvent)
            .where(
                PrintUsageEvent.archive_id == archive_id,
                PrintUsageEvent.event == EVENT_PAUSE,
            )
            .order_by(PrintUsageEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_pause is None:
        return None
    return {"archive_id": archive_id, "mode": "optin", "pause_layer": last_pause.layer_num or 0}


async def note_manual_replacement_intent(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
) -> bool:
    """The human declared the assignment that follows a REPLACEMENT — a
    deliberate mid-pause spool change with no firmware event to witness it.

    Journals a ``manual`` runout with the outgoing spool frozen from the
    still-current assignment — call BEFORE the assignment is rewritten — so
    the assignment that follows closes it as the ``spool_loaded`` boundary.
    ``manual`` shares the ambiguous contract: a boundary only through
    spool_loaded, never a zero correction (a preventively swapped reel is
    not empty). Accepted inside the ``manual_replacement_window`` — paused
    now, or resumed from a pause this print — with the boundary on the pause
    layer; returns False (nothing journaled) otherwise so the caller can
    refuse the flag loudly instead of mis-journaling a correction.
    """
    from backend.app.models.print_usage_event import KIND_MANUAL

    # Defence in depth: an older client can still send the flag, and journaling
    # a boundary that names nobody is worse than ignoring the claim.
    window = await manual_replacement_window(db, printer_id, ams_id=ams_id, tray_id=tray_id)
    if window is None:
        return False
    archive_id = window["archive_id"]

    if ams_id >= 254:
        global_tray = 254 + tray_id
    elif ams_id >= 128:
        global_tray = ams_id
    else:
        global_tray = ams_id * 4 + tray_id

    spool_id, spoolman_spool_id = await freeze_spool_ids(db, printer_id, global_tray)
    logger.info(
        "[UsageJournal] Manual replacement declared for tray %d on printer %d (outgoing spool=%s/%s, layer=%s)",
        global_tray,
        printer_id,
        spool_id,
        spoolman_spool_id,
        window["pause_layer"],
    )
    await record_runout(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=window["pause_layer"],
        kind=KIND_MANUAL,
        global_tray_id=global_tray,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )
    return True


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
