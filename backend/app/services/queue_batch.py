"""Helpers for creating grouped batches of queue items (quantity > 1)."""

import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.queue_counters import update_queue_counters


async def enqueue_batch_copies(
    db: AsyncSession,
    *,
    printer_id: int,
    count: int,
    archive_id: int | None = None,
    library_file_id: int | None = None,
    plate_id: int | None = None,
    ams_mapping: list[int] | None = None,
    bed_levelling: bool = True,
    flow_cali: bool = True,
    vibration_cali: bool = False,
    layer_inspect: bool = False,
    timelapse: bool = False,
    use_ams: bool = True,
    auto_off_after: bool = False,
    created_by_id: int | None = None,
    batch_id: str | None = None,
) -> tuple[list[PrintQueueItem], str | None]:
    """Append ``count`` identical pending items to the given printer's queue.

    Used by direct-print endpoints to queue up the extra copies after the first
    is dispatched. Returns (items, batch_id). If count <= 0, returns ([], None).
    """
    if count <= 0:
        return [], None

    # Resolve printer's queue
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return [], None

    result = await db.execute(
        select(func.max(PrintQueueItem.position))
        .where(PrintQueueItem.queue_id == queue.id)
        .where(PrintQueueItem.status == "pending")
    )
    max_pos = result.scalar() or 0

    if batch_id is None:
        batch_id = str(uuid.uuid4())
    ams_mapping_json = json.dumps(ams_mapping) if ams_mapping else None

    items: list[PrintQueueItem] = []
    for i in range(count):
        items.append(
            PrintQueueItem(
                queue_id=queue.id,
                archive_id=archive_id,
                library_file_id=library_file_id,
                ams_mapping=ams_mapping_json,
                plate_id=plate_id,
                bed_levelling=bed_levelling,
                flow_cali=flow_cali,
                vibration_cali=vibration_cali,
                layer_inspect=layer_inspect,
                timelapse=timelapse,
                use_ams=use_ams,
                auto_off_after=auto_off_after,
                position=max_pos + 1 + i,
                status="pending",
                batch_id=batch_id,
                created_by_id=created_by_id,
            )
        )
    db.add_all(items)
    await db.commit()
    for it in items:
        await db.refresh(it)

    await update_queue_counters(db, queue.id)
    await db.commit()
    return items, batch_id
