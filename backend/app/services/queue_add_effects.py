"""Provider and Telegram announcements for a committed operator action."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.archive import sd_stem

logger = logging.getLogger(__name__)


async def _send(db: AsyncSession, job_name: str, target: str, printer_id: int | None) -> None:
    from backend.app.services.notification_service import notification_service

    await notification_service.on_queue_job_added(
        job_name=sd_stem(job_name),
        target=target,
        db=db,
        printer_id=printer_id,
        printer_name=target if printer_id is not None else None,
    )


async def announce_queue_add(
    job_name: str, printer_id: int | None = None, *, target: str | None = None, sessions=None
) -> None:
    """Best effort, on a fresh session after the operator's action commits."""
    try:
        async with (sessions or database.async_session)() as db:
            if target is None:
                target = (
                    "AutoQueue"
                    if printer_id is None
                    else await db.scalar(select(Printer.name).where(Printer.id == printer_id))
                )
            if target is not None:
                await _send(db, job_name, target, printer_id)
    except Exception:
        logger.debug("Queue add notification failed", exc_info=True)


def _job_name(item: PrintQueueItem) -> str:
    return (
        (item.source_snapshot or {}).get("display_filename")
        or (item.archive.filename if item.archive else None)
        or (item.library_file.filename if item.library_file else None)
        or f"Job #{item.id}"
    )


async def announce_printer_queue_add(item: PrintQueueItem, queue: PrinterQueue) -> None:
    """The first created row, loaded with its sources and its queue's printer."""
    # MQTT queue/job_added is the relay's own event for committed rows.
    try:
        target = queue.printer.name if queue.printer else f"Queue #{queue.id}"
        await announce_queue_add(_job_name(item), queue.printer_id, target=target)
    except Exception:
        logger.debug("Notification of queue job %s failed", item.id, exc_info=True)


async def announce_printer_queue_item(item_id: int) -> None:
    """Read the committed row for doors that don't otherwise load its relationships."""
    try:
        async with database.async_session() as db:
            item = await db.scalar(
                select(PrintQueueItem)
                .where(PrintQueueItem.id == item_id)
                .options(
                    selectinload(PrintQueueItem.archive),
                    selectinload(PrintQueueItem.library_file),
                    selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
                )
            )
            if item is not None and item.queue is not None:
                target = item.queue.printer.name if item.queue.printer else f"Queue #{item.queue.id}"
                await _send(db, _job_name(item), target, item.queue.printer_id)
    except Exception:
        logger.debug("Notification of queue job %s failed", item_id, exc_info=True)
