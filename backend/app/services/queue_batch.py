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
    layer_inspect: bool = False,
    timelapse: bool = False,
    use_ams: bool = True,
    mesh_mode_fast_check: bool = True,
    execute_swap_macros: bool = False,
    swap_macro_events: list[str] | None = None,
    auto_off_after: bool = False,
    created_by_id: int | None = None,
    project_id: int | None = None,
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
    swap_macro_events_json = json.dumps(swap_macro_events) if execute_swap_macros and swap_macro_events else None

    # Fallback: inherit project_id from the library file if caller didn't pass
    # an explicit one — matches the cascade logic in the single-add endpoint so
    # project stats stay correct for direct-dispatch-with-quantity>1 paths.
    effective_project_id = project_id
    if effective_project_id is None and library_file_id is not None:
        from backend.app.models.library import LibraryFile

        lib_row = await db.get(LibraryFile, library_file_id)
        if lib_row is not None and lib_row.project_id is not None:
            effective_project_id = lib_row.project_id

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
                layer_inspect=layer_inspect,
                timelapse=timelapse,
                use_ams=use_ams,
                mesh_mode_fast_check=mesh_mode_fast_check,
                execute_swap_macros=execute_swap_macros,
                swap_macro_events=swap_macro_events_json,
                auto_off_after=auto_off_after,
                position=max_pos + 1 + i,
                status="pending",
                batch_id=batch_id,
                created_by_id=created_by_id,
                project_id=effective_project_id,
            )
        )
    db.add_all(items)
    await db.commit()
    for it in items:
        await db.refresh(it)

    await update_queue_counters(db, queue.id)
    await db.commit()
    return items, batch_id
