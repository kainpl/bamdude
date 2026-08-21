"""The finished row that is waiting for the operator to answer.

A print on a printer that confirms its plate leaves a question behind: clear it
and move on, or print it again. Until that is answered the queue row stays —
still ``completed``, because it is; what is outstanding is the acknowledgement,
and that lives on the PRINTER as ``awaiting_plate_clear``. So the waiting state
is the pair *(a completed row on this queue) + (the printer is awaiting plate
clear)*, and it needs no status value of its own.

⚠️ Keeping the row ``completed`` rather than inventing a status is also what
lets ``PrintScheduler.previous_print_succeeded`` finally see a success. That
lookback reads ``print_queue`` for the newest terminal row, and a successful one
used to be deleted the moment it completed — so a later success did not release
the ``require_previous_success`` gate, contrary to its own documentation. A new
status value would have been just as invisible to it.

⚠️ Deliberately no sweeper and no timeout. Printing all day, switching the
printers off at night and answering in the morning is the normal working day,
not a leak.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def should_hold_for_plate_clear(db: AsyncSession, printer_id: int, *, plate_auto_cleared: bool) -> bool:
    """Whether this printer's finished row waits for an answer.

    Mirrors the condition that arms the plate gate in ``on_print_complete``, and
    must keep mirroring it: a row held where no gate arms is a row nobody is
    ever asked about, and a gate armed over a row already deleted is the
    behaviour this replaces.
    """
    if plate_auto_cleared:
        return False
    require = await db.scalar(select(Printer.require_plate_clear).where(Printer.id == printer_id))
    return bool(require)


async def answer_by_clearing(db: AsyncSession, printer_id: int) -> int:
    """The operator took the part off: drop the row and let the queue move on.

    This is what used to happen the moment the print ended, only later — which
    is the whole change. Returns how many rows went, so a caller can log it.

    ⚠️ Silent when nothing is waiting, and that path is common: a swap printer,
    a gate armed by the reconnect sweep with no row behind it, or a farm that
    upgraded mid-print. Clear plate must never fail because there was nothing to
    tidy.
    """
    from backend.app.services.queue_counters import detach_print_queue_refs, update_queue_counters

    row = await waiting_row(db, printer_id)
    if row is None:
        return 0

    queue_id = row.queue_id
    row_id = row.id
    await detach_print_queue_refs(db, [row_id])
    await db.delete(row)
    await update_queue_counters(db, queue_id)
    await db.commit()
    logger.info("Plate cleared on printer %s — dropped the finished queue row %s", printer_id, row_id)
    return 1


async def answer_by_repeating(db: AsyncSession, printer_id: int) -> PrintQueueItem | None:
    """The operator took the part off and wants another: re-arm the same row.

    The same row, deliberately — not a copy. ``queue_ops.clone_item`` builds a
    new item from a hand-written field list, and that list had silently dropped
    ten print options before it was noticed; re-arming carries everything by
    construction, because it copies nothing.

    Returns the re-armed row, or None when nothing was waiting.

    ⚠️ Does NOT release the plate gate — the callers do, because they are the
    ones that know a person pressed something. While it is armed
    ``_is_printer_idle`` is False and the re-armed row would never dispatch.
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_top

    row = await waiting_row(db, printer_id)
    if row is None:
        return None

    row.status = "pending"
    # The archive belongs to the print that finished; this run will get its own.
    row.archive_id = None
    row.completed_at = None
    row.started_at = None
    row.error_message = None
    row.waiting_reason = None
    # ⚠️ The m108 cap counts attempts at dispatching THIS row. Left alone, a
    # series of repeats spends it and the row fails "after N attempts" although
    # every attempt printed.
    row.dispatch_attempts = 0
    await db.flush()

    # "Again" means now: front of the queue, renumbering the rest properly.
    await bump_block_to_top(db, row.queue_id, [row.id])
    await update_queue_counters(db, row.queue_id)
    await db.commit()
    await db.refresh(row)
    logger.info("Repeat requested on printer %s — re-armed queue row %s", printer_id, row.id)
    return row


async def waiting_row(db: AsyncSession, printer_id: int) -> PrintQueueItem | None:
    """The finished row this printer is waiting to be asked about, if any.

    Newest first: only one can be outstanding, but a farm that ran before this
    existed may hold older ones, and the operator answers about the last print.
    """
    return (
        (
            await db.execute(
                select(PrintQueueItem)
                .join(PrinterQueue, PrintQueueItem.queue_id == PrinterQueue.id)
                .where(PrinterQueue.printer_id == printer_id)
                .where(PrintQueueItem.status == "completed")
                .order_by(PrintQueueItem.completed_at.desc().nullslast(), PrintQueueItem.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
