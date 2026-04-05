"""API routes for printer queue management."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.schemas.printer_queue import PrinterQueueResponse, PrinterQueueUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queues", tags=["queues"])


def _to_response(queue: PrinterQueue) -> PrinterQueueResponse:
    return PrinterQueueResponse(
        id=queue.id,
        printer_id=queue.printer_id,
        printer_name=queue.printer.name if queue.printer else None,
        status=queue.status,
        last_activity_at=queue.last_activity_at,
        current_item_id=queue.current_item_id,
        pending_count=queue.pending_count,
        completed_count=queue.completed_count,
        failed_count=queue.failed_count,
        cancelled_count=queue.cancelled_count,
        skipped_count=queue.skipped_count,
        total_count=queue.total_count,
        created_at=queue.created_at,
        updated_at=queue.updated_at,
    )


@router.get("/", response_model=list[PrinterQueueResponse])
async def list_queues(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_READ),
):
    """List all printer queues with status and counters."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(PrinterQueue).options(selectinload(PrinterQueue.printer)).order_by(PrinterQueue.id)
    )
    queues = list(result.scalars().all())
    return [_to_response(q) for q in queues]


@router.get("/{queue_id}", response_model=PrinterQueueResponse)
async def get_queue(
    queue_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_READ),
):
    """Get a specific printer queue."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(PrinterQueue).options(selectinload(PrinterQueue.printer)).where(PrinterQueue.id == queue_id)
    )
    queue = result.scalar_one_or_none()
    if not queue:
        raise HTTPException(404, "Queue not found")
    return _to_response(queue)


@router.patch("/{queue_id}", response_model=PrinterQueueResponse)
async def update_queue(
    queue_id: int,
    data: PrinterQueueUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_UPDATE_ALL),
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

    await db.commit()
    await db.refresh(queue)
    return _to_response(queue)
