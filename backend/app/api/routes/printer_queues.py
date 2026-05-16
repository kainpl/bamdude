"""API routes for printer queue management."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.schemas.printer_queue import PrinterQueueResponse, PrinterQueueUpdate
from backend.app.services.queue_counters import get_queue_terminal_counts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queues", tags=["queues"])


def _to_response(queue: PrinterQueue, terminal_counts: dict[str, int]) -> PrinterQueueResponse:
    return PrinterQueueResponse(
        id=queue.id,
        printer_id=queue.printer_id,
        printer_name=queue.printer.name if queue.printer else None,
        printer_model=queue.printer.model if queue.printer else None,
        printer_location=queue.printer.location if queue.printer else None,
        status=queue.status,
        is_paused=queue.is_paused,
        last_activity_at=queue.last_activity_at,
        current_item_id=queue.current_item_id,
        pending_count=queue.pending_count,
        completed_count=terminal_counts.get("completed_count", 0),
        failed_count=terminal_counts.get("failed_count", 0),
        cancelled_count=terminal_counts.get("cancelled_count", 0),
        skipped_count=queue.skipped_count,
        total_count=terminal_counts.get("total_count", 0),
        created_at=queue.created_at,
        updated_at=queue.updated_at,
    )


async def _bulk_terminal_counts(db: AsyncSession, queue_ids: list[int]) -> dict[int, dict[str, int]]:
    """Compute terminal archive counters for every queue in one query.

    Replaces the cached ``completed_count``/``failed_count``/``cancelled_count``/
    ``total_count`` columns dropped in m019 — queue terminal state now lives
    on ``print_archives.queue_id`` and is aggregated at read time.
    """
    if not queue_ids:
        return {}

    result = await db.execute(
        select(PrintArchive.queue_id, PrintArchive.status, func.count())
        .where(PrintArchive.queue_id.in_(queue_ids))
        .group_by(PrintArchive.queue_id, PrintArchive.status)
    )
    by_queue: dict[int, dict[str, int]] = {qid: {} for qid in queue_ids}
    for qid, status, count in result.all():
        if qid is None:
            continue
        by_queue.setdefault(qid, {})[status] = int(count or 0)

    counts_by_queue: dict[int, dict[str, int]] = {}
    for qid, by_status in by_queue.items():
        completed = by_status.get("completed", 0)
        failed = by_status.get("failed", 0)
        # Treat the whole failure family as "cancelled" for the legacy split;
        # a dedicated failure breakdown still lives under ``failed_count``.
        cancelled = by_status.get("cancelled", 0) + by_status.get("aborted", 0) + by_status.get("stopped", 0)
        total = sum(by_status.values())
        counts_by_queue[qid] = {
            "completed_count": completed,
            "failed_count": failed,
            "cancelled_count": cancelled,
            "total_count": total,
        }
    return counts_by_queue


@router.get("/", response_model=list[PrinterQueueResponse])
async def list_queues(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """List all printer queues with status and counters."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(PrinterQueue).options(selectinload(PrinterQueue.printer)).order_by(PrinterQueue.id)
    )
    queues = list(result.scalars().all())
    counts_by_queue = await _bulk_terminal_counts(db, [q.id for q in queues])
    return [_to_response(q, counts_by_queue.get(q.id, {})) for q in queues]


@router.get("/{queue_id}", response_model=PrinterQueueResponse)
async def get_queue(
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """Get a specific printer queue."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(PrinterQueue).options(selectinload(PrinterQueue.printer)).where(PrinterQueue.id == queue_id)
    )
    queue = result.scalar_one_or_none()
    if not queue:
        raise HTTPException(404, "Queue not found")
    terminal_counts = await get_queue_terminal_counts(db, queue.id)
    return _to_response(queue, terminal_counts)


@router.patch("/{queue_id}", response_model=PrinterQueueResponse)
async def update_queue(
    queue_id: int,
    data: PrinterQueueUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Update queue status (pause/resume/clear error)."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(PrinterQueue).options(selectinload(PrinterQueue.printer)).where(PrinterQueue.id == queue_id)
    )
    queue = result.scalar_one_or_none()
    if not queue:
        raise HTTPException(404, "Queue not found")

    if data.status is not None:
        if data.status not in ("idle", "paused"):
            raise HTTPException(400, "Can only set status to 'idle' or 'paused'")
        # Don't allow pausing while printing
        if data.status == "paused" and queue.status == "printing":
            raise HTTPException(400, "Cannot pause queue while printing. Stop the print first.")
        queue.status = data.status
        queue.current_item_id = None
        queue.last_activity_at = datetime.now(timezone.utc)

    # is_paused is orthogonal to status: allowed in any state, including
    # while the queue is 'printing'. The running print is untouched — only
    # the next dispatch (and new-item adds) are gated.
    if data.is_paused is not None:
        queue.is_paused = data.is_paused
        queue.last_activity_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(queue)
    terminal_counts = await get_queue_terminal_counts(db, queue.id)
    return _to_response(queue, terminal_counts)
