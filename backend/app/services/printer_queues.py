"""Every printer has a queue row, and the queue's id is the printer's id.

``printer_queues`` used to be created in exactly one place — the HTTP
add-printer route — and assumed everywhere else. A printer added from the
Telegram bot, restored from a backup, or older than m002's seed had none, and
nothing downstream complained: the claim a print takes at start returned None,
the completion then found no row to close, and the plate gate armed regardless
— so the card offered "Repeat" for a print with no row to re-arm and the route
answered ``409 No finished print is waiting`` (reported 2026-09-04, a print
started from the printer's own screen).

Two guards, both idempotent: ``ensure_printer_queue`` wherever a queue is
first needed, and ``ensure_all_printer_queues`` once at startup.

⚠️ ``id == printer_id`` is the PREFERENCE, not a guarantee. SQLite here never
runs with ``PRAGMA foreign_keys = ON``, so a deleted printer's queue can outlive
it and sit on the id a new printer later receives; the fallback is the next
free id. Nothing may assume the two are equal — look the queue up by
``PrinterQueue.printer_id`` (``main._printing_rows_for_printer`` does).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def ensure_printer_queue(db: AsyncSession, printer_id: int) -> PrinterQueue:
    """The printer's queue row, created under the printer's own id if missing.

    Flushes but does not commit — the caller owns the transaction, so the row
    lands together with whatever needed it (an archive, a claim, the printer).
    """
    queue = (await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))).scalar_one_or_none()
    if queue is not None:
        return queue

    squatter = await db.get(PrinterQueue, printer_id)
    if squatter is None:
        queue = PrinterQueue(id=printer_id, printer_id=printer_id)
    else:
        logger.warning(
            "printer %s has no queue and queue id %s is taken by printer %s — creating one under the next free id",
            printer_id,
            printer_id,
            squatter.printer_id,
        )
        queue = PrinterQueue(printer_id=printer_id)
    db.add(queue)
    await db.flush()
    logger.info("Created queue %s for printer %s", queue.id, printer_id)
    return queue


async def ensure_all_printer_queues(db: AsyncSession) -> int:
    """Create a queue for every printer that has none. Returns how many were created."""
    with_queue = select(PrinterQueue.printer_id)
    missing = (await db.execute(select(Printer.id).where(Printer.id.not_in(with_queue)))).scalars().all()
    for printer_id in missing:
        await ensure_printer_queue(db, printer_id)
    if missing:
        await db.commit()
        logger.info("Startup: created queues for %d printer(s) that had none: %s", len(missing), list(missing))
    return len(missing)
