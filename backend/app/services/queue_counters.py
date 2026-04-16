"""Queue counter management - keeps PrinterQueue cached counters in sync."""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def update_queue_counters(db: AsyncSession, queue_id: int) -> None:
    """Recount all cached counters for a queue from actual item statuses."""
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == queue_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return

    # Count each status in one query
    counts = {}
    for status in ("pending", "completed", "failed", "cancelled", "skipped"):
        result = await db.execute(
            select(func.count())
            .select_from(PrintQueueItem)
            .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == status)
        )
        counts[status] = result.scalar() or 0

    total_result = await db.execute(
        select(func.count()).select_from(PrintQueueItem).where(PrintQueueItem.queue_id == queue_id)
    )

    queue.pending_count = counts["pending"]
    queue.completed_count = counts["completed"]
    queue.failed_count = counts["failed"]
    queue.cancelled_count = counts["cancelled"]
    queue.skipped_count = counts["skipped"]
    queue.total_count = total_result.scalar() or 0
    queue.last_activity_at = datetime.now(timezone.utc)


async def set_queue_error(db: AsyncSession, queue_id: int, failed_item_id: int | None = None) -> None:
    """Set queue to error status when a print fails."""
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == queue_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return

    queue.status = "error"
    queue.current_item_id = None
    queue.last_activity_at = datetime.now(timezone.utc)
    logger.info("Queue %d set to error state (failed item: %s)", queue_id, failed_item_id)


async def set_queue_printing(db: AsyncSession, queue_id: int, item_id: int) -> None:
    """Set queue to printing status when a print starts."""
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == queue_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return

    queue.status = "printing"
    queue.current_item_id = item_id
    queue.last_activity_at = datetime.now(timezone.utc)


async def set_queue_idle(db: AsyncSession, queue_id: int) -> None:
    """Set queue to idle status when a print completes successfully."""
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == queue_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return

    # Only set idle if currently printing (don't override paused/error)
    if queue.status == "printing":
        queue.status = "idle"
    queue.current_item_id = None
    queue.last_activity_at = datetime.now(timezone.utc)
