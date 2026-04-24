"""Queue counter management — keeps PrinterQueue's live-state counters in sync.

Post-m019, terminal counters (``completed`` / ``failed`` / ``cancelled`` /
``total``) live in ``print_archives`` via ``archive.queue_id`` and are
computed on the read path by :func:`get_queue_terminal_counts` below.
Only ``pending_count`` and ``skipped_count`` stay cached on
``PrinterQueue`` — they track live queue items that aren't auto-cleaned.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def update_queue_counters(db: AsyncSession, queue_id: int) -> None:
    """Recount the live-state counters (pending + skipped) for a queue.

    Terminal counters moved to :func:`get_queue_terminal_counts` (archive-backed).
    """
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == queue_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return

    counts = {}
    for status in ("pending", "skipped"):
        result = await db.execute(
            select(func.count())
            .select_from(PrintQueueItem)
            .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == status)
        )
        counts[status] = result.scalar() or 0

    queue.pending_count = counts["pending"]
    queue.skipped_count = counts["skipped"]
    queue.last_activity_at = datetime.now(timezone.utc)


async def get_queue_terminal_counts(db: AsyncSession, queue_id: int) -> dict[str, int]:
    """Count terminal-status archives for a queue.

    Read path for ``GET /printer-queues/`` — replaces the removed cached
    columns (``completed_count``/``failed_count``/``cancelled_count``/
    ``total_count``). Treats the whole failure family (``failed`` /
    ``aborted`` / ``cancelled`` / ``stopped``) as "cancelled" for the
    legacy display split, with a dedicated ``failed`` breakdown as well.
    """
    result = await db.execute(
        select(PrintArchive.status, func.count()).where(PrintArchive.queue_id == queue_id).group_by(PrintArchive.status)
    )
    by_status = {row[0]: int(row[1] or 0) for row in result.all()}

    completed = by_status.get("completed", 0)
    failed = by_status.get("failed", 0)
    cancelled = by_status.get("cancelled", 0) + by_status.get("aborted", 0) + by_status.get("stopped", 0)
    total = sum(by_status.values())

    return {
        "completed_count": completed,
        "failed_count": failed,
        "cancelled_count": cancelled,
        "total_count": total,
    }


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


async def set_queue_printing(db: AsyncSession, queue_id: int, item_id: int | None = None) -> None:
    """Set queue to printing status when a print starts.

    ``item_id`` may be None for prints that have no corresponding
    ``PrintQueueItem`` row (external prints, BamDude direct dispatch).
    In that case the queue still transitions to "printing" so the UI
    reflects the real printer state — the queue widget then synthesises
    a virtual current-print item for display.

    **Do not** clobber ``current_item_id`` with None when the scheduler
    already set it to a valid queue-item id earlier in the dispatch
    flow. Previously ``on_print_start`` paths called this with no
    ``item_id`` and wiped the value; the UI then lost track of which
    queued item was live. If a caller explicitly wants to clear the
    pointer (terminal transition), use :func:`set_queue_idle` instead.
    """
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == queue_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return

    queue.status = "printing"
    if item_id is not None:
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
