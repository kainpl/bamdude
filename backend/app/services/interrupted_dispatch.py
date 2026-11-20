"""A dispatch attempt whose owner ended before it was sent goes back to the queue — only by proof (post-rebuild I-R3).

An execution archive carries its dispatch phase and the boot of the process that owned it
(``services/dispatch_phase``). When THIS process holds the installation's owner lock
(``farm_startup_guard``) and an attempt names another boot, its owner has ended: two
processes never hold that lock together. That is the only proof used — never an age, a
timer or a file name.

Of such attempts only a ``prepared`` one — the start command was provably never sent — may
return to ``pending`` by itself, and only when all of these hold at the moment of the move:

* the farm's crash-start pause is released (otherwise it stays visible and this check runs
  again after Resume);
* the printer reports a fresh, inactive state, with no other print archive running on it;
* our swap-start macro provably did not run (``swap_terminal.cycle_state`` is
  ``not_started``); one that ran, or may have, fails the attempt and halts the queue, so a
  table is never moved twice;
* no other unfinished effect is recorded on the archive (a pending-events list is only an
  intent and proves nothing);
* the row still has dispatch attempts to spend; otherwise it fails for the operator's Retry.

``sending`` / ``sent`` / unknown attempts are never moved here: the start may be on the
printer, so they are the operator's to close through the Info action. Nothing here sends a
physical command, replays a completion, books usage or touches the heaters. Recovery never
gives the spent attempt back: a crash is not a typed refusal (D-R5).

The check is addressed and has no timer: it runs when a printer's first fresh status of a
connection arrives (``reconcile_printer_prints``) and when the farm is resumed. When that
look finds a candidate but the printer is not yet a fresh inactive one (busy, stale), the
printer waits here; its next status that shows it fresh and idle runs the check once more
(``on_status``) — in the same connection, with no new Resume or reconnect.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import dispatch_phase
from backend.app.utils import failure_reasons

logger = logging.getLogger(__name__)

REQUEUED = "requeued"
FAILED = "failed"

_INTERRUPTED = "The dispatch was interrupted before the start command was sent"

_INACTIVE_STATES = frozenset({"IDLE", "FINISH", "FAILED"})
#: Printers whose recheck found a candidate while the printer was not yet a fresh inactive
#: one: their next fresh idle status runs it once more (``on_status``).
_waiting: set[int] = set()


def recheck_due(printer_id: int) -> bool:
    """A printer that waits for its recheck now reads as a fresh inactive one: due, once."""
    from backend.app.services.print_state_recovery_store import live_snapshot

    if printer_id not in _waiting or live_snapshot(printer_id)["mode"] != "inactive":
        return False
    _waiting.discard(printer_id)
    return True


def on_status(printer_id: int, state) -> None:
    """Every printer status, from main's status callback: a waiting recheck runs once, when due.

    Costs nothing for a printer nobody waits for, and reads no database: only a status that
    shows a waiting printer idle asks the in-memory snapshot. Never raises.
    """
    if printer_id not in _waiting or getattr(state, "state", None) not in _INACTIVE_STATES:
        return
    try:
        if not recheck_due(printer_id):
            return
        from backend.app.core.tasks import spawn_background_task

        spawn_background_task(_recheck(printer_id), name=f"interrupted-dispatch-recheck-{printer_id}")
    except Exception:  # noqa: BLE001 - a status callback never fails over a recheck
        logger.exception("Interrupted dispatch recheck could not start for printer %s", printer_id)


async def _recheck(printer_id: int) -> None:
    try:
        await recover_interrupted_dispatches(printer_id)
    except Exception:  # noqa: BLE001 - Info still shows the attempt
        logger.exception("Interrupted dispatch recheck failed for printer %s", printer_id)


async def _candidates(db, printer_id: int, boot: str) -> tuple[int | None, list]:
    queue_id = await db.scalar(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
    if queue_id is None:
        return None, []
    rows = (
        await db.execute(
            select(PrintQueueItem.id, PrintArchive.id)
            .join(PrintArchive, PrintArchive.id == PrintQueueItem.archive_id)
            .where(
                PrintQueueItem.queue_id == queue_id,
                PrintQueueItem.status == "printing",
                PrintArchive.status == "printing",
                PrintArchive.dispatch_phase == dispatch_phase.PREPARED,
                PrintArchive.dispatch_boot_id.is_not(None),
                PrintArchive.dispatch_boot_id != boot,
            )
            .order_by(PrintQueueItem.id)
        )
    ).all()
    return queue_id, rows


async def recover_interrupted_dispatches(printer_id: int) -> list[str]:
    """Settle the ``prepared`` attempts of an ended owner on this printer; the outcome of each, in order."""
    from backend.app.services.farm_startup_guard import farm_startup_guard
    from backend.app.services.print_state_recovery_store import live_snapshot

    if not farm_startup_guard.allowed():
        return []  # the farm's Resume runs this again
    boot = dispatch_phase.current_boot_id()
    if boot is None:
        return []
    ready = live_snapshot(printer_id)["mode"] == "inactive"
    async with database.async_session() as db:
        queue_id, candidates = await _candidates(db, printer_id, boot)
    if not candidates:
        _waiting.discard(printer_id)
        return []
    if not ready:
        _waiting.add(printer_id)
        return []
    _waiting.discard(printer_id)
    outcomes = []
    for item_id, archive_id in candidates:
        outcome = await _settle(printer_id, queue_id, item_id, archive_id, boot)
        if outcome:
            outcomes.append(outcome)
    return outcomes


async def _settle(printer_id: int, queue_id: int, item_id: int, archive_id: int, boot: str) -> str | None:
    from backend.app.services import queue_source_link, swap_terminal
    from backend.app.services.archive_write_scope import load_active_archive_for_write
    from backend.app.services.print_scheduler import DISPATCH_MAX_ATTEMPTS
    from backend.app.services.print_state_recovery import effects_pending
    from backend.app.services.queue_counters import halt_lane, update_queue_counters
    from backend.app.services.queue_ops import claim_lanes

    # The source link a requeue writes, planned from a read before the guards (G03 B, A27): its parent is in
    # the plan — a library row as an early row, an archive with the execution archive, ascending.
    async with database.async_session() as preflight:
        read = await preflight.get(PrintQueueItem, item_id)
        link_plan = queue_source_link.plan_link(read) if read is not None else queue_source_link.LinkPlan(None, False)
    parent_archive = (
        link_plan.candidate.id
        if link_plan.needs_parent and link_plan.candidate.kind == queue_source_link.ARCHIVE
        else None
    )

    async def library_first(session):
        if link_plan.needs_parent and link_plan.candidate.kind == queue_source_link.LIBRARY_FILE:
            await queue_source_link.lock_plans(session, [link_plan])

    archives = sorted({archive_id, *([parent_archive] if parent_archive is not None else [])})
    # Archive before lane before rows, the order every archive + queue writer keeps; the rows
    # archive → queue → job, as operator print-state recovery takes them.
    async with (
        database.async_session() as db,
        claim_lanes(db, [queue_id], archive_ids=archives, before_db=library_first),
    ):
        archive = None
        for each in archives:
            if each == archive_id:
                archive = await load_active_archive_for_write(db, archive_id)
            else:
                await queue_source_link.lock_plans(db, [link_plan])
        queue = await db.scalar(
            select(PrinterQueue)
            .where(PrinterQueue.id == queue_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        item = await db.scalar(
            select(PrintQueueItem)
            .where(PrintQueueItem.id == item_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if (
            archive is None
            or item is None
            or queue is None
            or archive.status != "printing"
            or archive.dispatch_phase != dispatch_phase.PREPARED
            or archive.dispatch_boot_id in (None, boot)
            or item.status != "printing"
            or item.archive_id != archive_id
            or item.queue_id != queue_id
        ):
            return None
        others = await db.scalar(
            select(func.count(PrintArchive.id)).where(
                PrintArchive.printer_id == printer_id,
                PrintArchive.status == "printing",
                PrintArchive.deleted_at.is_(None),
                PrintArchive.id != archive_id,
            )
        )
        if others:
            return None  # another print may be on this printer: the operator decides
        start_ran = swap_terminal.cycle_state(archive.extra_data) != swap_terminal.CYCLE_NOT_STARTED
        if not start_ran and effects_pending({"extra_data": archive.extra_data}, intents=False):
            return None  # an unfinished effect is recorded: visible in Info, never resolved here
        now = datetime.now(timezone.utc)
        code = failure_reasons.DISPATCH_PREPARATION_ERROR
        # A direct print ("print now") is never started later by itself: like a refused one,
        # it ends, and leaves the queue to the jobs behind it.
        direct = item.origin == "direct"
        if start_ran:
            detail = f"{_INTERRUPTED}, after the swap start macro ran or may have. Check the plate, then Retry."
        elif direct:
            detail = f"{_INTERRUPTED}; a direct print is not started again by itself. Check the printer, then Retry."
        elif (item.dispatch_attempts or 0) >= DISPATCH_MAX_ATTEMPTS:
            detail = f"{_INTERRUPTED}, and its dispatch attempts are spent. Check the printer, then Retry."
        else:
            detail = None

        archive.status = "failed"
        archive.failure_reason = code
        archive.error_message = detail or f"{_INTERRUPTED}; the job went back to the queue."
        archive.completed_at = now
        if detail is None:
            # Back to pending with its own bytes, options and macros; the attempt it spent stays spent. Its link
            # names the proven original, never its own failed attempt — or nothing (A27); a candidate that changed
            # since the plan leaves the row for the operator.
            if not queue_source_link.apply_link(item, link_plan, execution_archive_id=archive_id):
                return None
            from backend.app.services.filament_policy import clear_routing_runtime

            clear_routing_runtime(item)
            item.status = "pending"
            item.started_at = None
            item.error_message = None
            item.error_code = None
            item.waiting_reason = None
            if queue.current_item_id == item_id and queue.status == "printing":
                queue.status = "idle"
                queue.current_item_id = None
        else:
            item.status = "failed"
            item.error_message = detail
            item.error_code = code
            item.completed_at = now
            # It never printed: no physical failure for a require-previous-success gate.
            item.gate_acknowledged = True
            if direct and not start_ran:
                if queue.current_item_id == item_id and queue.status == "printing":
                    queue.status = "idle"
                    queue.current_item_id = None
            else:
                await halt_lane(db, queue_id, item_id=item_id, kind="error")
        await update_queue_counters(db, queue_id)
        await db.commit()
    outcome = REQUEUED if detail is None else FAILED
    logger.warning(
        "Interrupted dispatch: printer %d item %s archive %s (phase prepared, owner ended) — %s",
        printer_id,
        item_id,
        archive_id,
        outcome,
    )
    try:
        from backend.app.core.websocket import ws_manager

        await ws_manager.send_queue_changed(printer_id)
        await ws_manager.send_archive_updated({"id": archive_id})
    except Exception:  # noqa: BLE001 - committed; the next refresh shows it
        logger.warning("Interrupted dispatch settled; event delivery failed for printer %s", printer_id)
    return outcome


async def recover_after_resume() -> None:
    """The addressed recheck the farm's Resume owes: every printer that has a candidate, once."""
    async with database.async_session() as db:
        printers = (
            await db.scalars(
                select(PrinterQueue.printer_id)
                .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
                .join(PrintArchive, PrintArchive.id == PrintQueueItem.archive_id)
                .where(
                    PrintQueueItem.status == "printing",
                    PrintArchive.status == "printing",
                    PrintArchive.dispatch_phase == dispatch_phase.PREPARED,
                )
                .distinct()
            )
        ).all()
    for printer_id in printers:
        try:
            await recover_interrupted_dispatches(printer_id)
        except Exception:  # noqa: BLE001 - one printer's recheck never stops another's
            logger.exception("Interrupted dispatch recheck failed for printer %s", printer_id)
