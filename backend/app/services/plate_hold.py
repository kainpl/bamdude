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
