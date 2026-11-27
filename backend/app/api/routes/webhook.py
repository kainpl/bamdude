import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import check_printer_access, check_webhook_permission, get_api_key
from backend.app.core.database import get_db
from backend.app.models.api_key import APIKey
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_control import stop_print_by_user
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# Request schemas
class PrinterStatusResponse(BaseModel):
    id: int
    name: str
    connected: bool
    state: str | None
    current_print: str | None
    progress: float | None
    remaining_time: int | None


class QueueStatusResponse(BaseModel):
    printer_id: int
    printer_name: str
    pending: int
    printing: int
    items: list[dict]


# Webhook endpoints


@router.post("/printer/{printer_id}/start")
async def webhook_start_print(
    printer_id: int,
    api_key: APIKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Start the next queued print on a printer.

    Requires 'can_control_printer' permission.

    The call is the operator's "go" for a job waiting on a manual start, and nothing more: a paused
    queue, a plate nobody confirmed as cleared and a job scheduled for later hold it exactly as they
    hold the queue's own start. The answer says whether the print was started.
    """
    await check_webhook_permission(db, api_key, "control_printer")
    await check_printer_access(db, api_key, printer_id)

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    queue = (await db.execute(select(PrinterQueue).where(PrinterQueue.id == printer_id))).scalar_one_or_none()
    if queue is not None and (queue.is_paused or queue.status in ("paused", "error")):
        raise HTTPException(status_code=409, detail="The printer's queue is paused")

    # Get next pending queue item that is due (a job scheduled for later is not the next one)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await db.execute(
        select(PrintQueueItem)
        .where(
            PrintQueueItem.queue_id == printer_id,
            PrintQueueItem.status == "pending",
            or_(PrintQueueItem.scheduled_time.is_(None), PrintQueueItem.scheduled_time <= now),
        )
        .order_by(PrintQueueItem.position, PrintQueueItem.id)
        .limit(1)
    )
    queue_item = result.scalar_one_or_none()
    if not queue_item:
        raise HTTPException(status_code=404, detail="No pending prints in queue")

    # Check if printer is ready. get_status() returns a PrinterState
    # dataclass, not a dict — use attribute access.
    status = printer_manager.get_status(printer_id)
    if not status or not status.connected:
        raise HTTPException(status_code=503, detail="Printer not connected")

    if status.state not in ("IDLE", "FINISH", "FAILED"):
        raise HTTPException(status_code=409, detail=f"Printer is busy (state: {status.state})")

    if printer_manager.is_awaiting_plate_clear(printer_id):
        raise HTTPException(status_code=409, detail="The plate has not been cleared")

    # Dispatch through the scheduler's start path — the single dispatch
    # layer that uploads the file over FTP, patches the 3MF and issues the
    # MQTT start command. The previous code called printer_manager.start_print
    # with the archive id where a filename was expected and never uploaded
    # the file, so this endpoint could never actually start a print.
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    item_id = queue_item.id
    try:
        started = await print_scheduler._start_print(db, queue_item)
    except Exception as e:
        logger.error("Failed to start print for queue item %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail=str(e))

    if not started:
        # The start path refused and wrote why on the row (a wait reason, or the failure).
        row = (
            await db.execute(
                select(PrintQueueItem.waiting_reason, PrintQueueItem.error_message)
                .where(PrintQueueItem.id == item_id)
                .execution_options(populate_existing=True)
            )
        ).first()
        reason = next((text for text in (row or ()) if text), None)
        if reason:
            raise HTTPException(status_code=409, detail=f"The print was not started: {reason}")
        raise HTTPException(status_code=409, detail="The print was not started")

    return {"message": "Print started", "queue_item_id": item_id}


@router.post("/printer/{printer_id}/stop")
async def webhook_stop_print(
    printer_id: int,
    api_key: APIKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Stop the current print on a printer.

    Requires 'can_control_printer' permission.
    """
    await check_webhook_permission(db, api_key, "control_printer")
    await check_printer_access(db, api_key, printer_id)

    status = printer_manager.get_status(printer_id)
    if not status or not status.connected:
        raise HTTPException(status_code=503, detail="Printer not connected")

    if status.state not in ("RUNNING", "PAUSE"):
        raise HTTPException(status_code=409, detail="No print in progress")

    # re-Connect MQTT if stalled
    if not await printer_manager.ensure_fresh_connection(printer_id):
        raise HTTPException(status_code=503, detail="Can`t re-connect printer MQTT")

    try:
        success = stop_print_by_user(printer_id)
        if not success:
            raise HTTPException(status_code=503, detail="Failed to send stop command")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to stop print: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return {"message": "Print stopped"}


@router.post("/printer/{printer_id}/cancel")
async def webhook_cancel_print(
    printer_id: int,
    api_key: APIKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Cancel the current print on a printer.

    Requires 'can_control_printer' permission.
    """
    await check_webhook_permission(db, api_key, "control_printer")
    await check_printer_access(db, api_key, printer_id)

    status = printer_manager.get_status(printer_id)
    if not status or not status.connected:
        raise HTTPException(status_code=503, detail="Printer not connected")

    if status.state not in ("RUNNING", "PAUSE"):
        raise HTTPException(status_code=409, detail="No print to cancel")

    # re-Connect MQTT if stalled
    if not await printer_manager.ensure_fresh_connection(printer_id):
        raise HTTPException(status_code=503, detail="Can`t re-connect printer MQTT")

    try:
        success = stop_print_by_user(printer_id)
        if not success:
            raise HTTPException(status_code=503, detail="Failed to send stop command")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to cancel print: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return {"message": "Print cancelled"}


@router.get("/printer/{printer_id}/status", response_model=PrinterStatusResponse)
async def webhook_get_printer_status(
    printer_id: int,
    api_key: APIKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get status of a printer.

    Requires 'can_read_status' permission.
    """
    await check_webhook_permission(db, api_key, "read_status")
    await check_printer_access(db, api_key, printer_id)

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    status = printer_manager.get_status(printer_id)

    return PrinterStatusResponse(
        id=printer.id,
        name=printer.name,
        connected=status.connected if status else False,
        state=status.state if status else None,
        current_print=status.current_print if status else None,
        progress=status.progress if status else None,
        remaining_time=status.remaining_time if status else None,
    )


@router.get("/queue", response_model=list[QueueStatusResponse])
async def webhook_get_queue_status(
    printer_id: int | None = None,
    api_key: APIKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get queue status for all printers or a specific printer.

    Requires 'can_read_status' permission.
    """
    await check_webhook_permission(db, api_key, "read_status")

    # Get printers
    if printer_id:
        await check_printer_access(db, api_key, printer_id)
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printers = result.scalars().all()
    else:
        # A retired printer is hidden everywhere; naming it by id above still answers.
        result = await db.execute(select(Printer).where(Printer.archived.is_(False)).order_by(Printer.id))
        printers = result.scalars().all()
        # The key's scope: its own list ∩ its owner's (N62 D5).
        from backend.app.core.auth import resolve_apikey_owner
        from backend.app.core.printer_scope import allows, key_scope

        scope = await key_scope(db, api_key, await resolve_apikey_owner(db, api_key))
        printers = [p for p in printers if allows(scope, p.id)]

    response = []
    for printer in printers:
        # Get queue items
        result = await db.execute(
            select(PrintQueueItem)
            .where(
                PrintQueueItem.queue_id == printer.id,
                PrintQueueItem.status.in_(["pending", "printing"]),
            )
            .order_by(PrintQueueItem.position)
        )
        items = result.scalars().all()

        pending_count = sum(1 for i in items if i.status == "pending")
        printing_count = sum(1 for i in items if i.status == "printing")

        response.append(
            QueueStatusResponse(
                printer_id=printer.id,
                printer_name=printer.name,
                pending=pending_count,
                printing=printing_count,
                items=[
                    {
                        "id": item.id,
                        "archive_id": item.archive_id,
                        "position": item.position,
                        "status": item.status,
                    }
                    for item in items
                ],
            )
        )

    return response
