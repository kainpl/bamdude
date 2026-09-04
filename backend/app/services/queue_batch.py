"""Helpers for creating queue items outside the ordinary add-to-queue route.

Two shapes live here: the grouped batch a quantity>1 direct print leaves behind,
and the single already-claimed row a direct print takes for itself.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.calibration_mode import normalize_mode
from backend.app.services.order_filing import resolve_line_id
from backend.app.services.queue_counters import set_queue_printing, update_queue_counters


def _item_columns(
    *,
    archive_id: int | None,
    library_file_id: int | None,
    options: dict | None,
    project_id: int | None,
    project_line_id: int | None = None,
) -> dict:
    """Dispatch options → queue-item columns.

    Reads with ``.get()`` because the caller's dict comes from
    ``model_dump(exclude_none=True)`` — an option left at its default is simply
    absent, not None.

    ⚠️ ``created_by_id`` is deliberately NOT in here. It is passed explicitly at
    each ``PrintQueueItem(...)`` site because ``test_queue_item_attribution``
    reads those sites with the AST and cannot see through a ``**`` unpack —
    hiding the owner in this dict would silence the guard for the whole module.
    """
    opts = options or {}
    bed_mode = normalize_mode(opts.get("bed_levelling", True))
    flow_mode = normalize_mode(opts.get("flow_cali", True))
    nozzle_mode = normalize_mode(opts.get("nozzle_offset_cali", True))
    ams_mapping = opts.get("ams_mapping")
    swap_events = opts.get("swap_macro_events")
    selected_macro_ids = opts.get("selected_macro_ids")
    execute_swap_macros = bool(opts.get("execute_swap_macros", False))

    return {
        "archive_id": archive_id,
        "library_file_id": library_file_id,
        "ams_mapping": json.dumps(ams_mapping) if ams_mapping else None,
        "plate_id": opts.get("plate_id"),
        "bed_levelling": bed_mode == "on",
        "bed_levelling_mode": bed_mode,
        "flow_cali": flow_mode == "on",
        "flow_cali_mode": flow_mode,
        "layer_inspect": bool(opts.get("layer_inspect", False)),
        "timelapse": bool(opts.get("timelapse", False)),
        "timelapse_storage": opts.get("timelapse_storage"),
        "use_ams": bool(opts.get("use_ams", True)),
        "nozzle_offset_cali": nozzle_mode == "on",
        "nozzle_offset_cali_mode": nozzle_mode,
        "mesh_mode_fast_check": bool(opts.get("mesh_mode_fast_check", True)),
        "gcode_injection": bool(opts.get("gcode_injection", False)),
        "execute_swap_macros": execute_swap_macros,
        "swap_macro_events": json.dumps(swap_events) if execute_swap_macros and swap_events else None,
        "selected_macro_ids": json.dumps(selected_macro_ids) if selected_macro_ids is not None else None,
        "auto_off_after": bool(opts.get("auto_off_after", False)),
        "project_id": project_id,
        "project_line_id": project_line_id,
    }


async def claim_printer_for_direct_print(
    db: AsyncSession,
    *,
    printer_id: int,
    origin: Literal["direct", "external"],
    archive_id: int | None = None,
    library_file_id: int | None = None,
    options: dict | None = None,
    created_by_id: int | None = None,
    project_id: int | None = None,
    project_line_id: int | None = None,
) -> PrintQueueItem | None:
    """Take the printer's queue claim for a print being dispatched right now.

    "Print now" used to claim nothing until ``on_print_start`` — i.e. until the
    printer had already started — so for the whole upload the queue saw an idle
    printer and dispatched over the job on its way. The row created here is the
    same claim the scheduler takes before its own dispatch, and it is read by
    the same ``PrinterQueue.status='printing'`` seed in ``check_queue``.

    ⚠️ ``status='printing'``, not ``pending``: the scheduler's dispatch flip is a
    compare-and-set gated on ``pending``, which is what stops this row being
    dispatched a second time. ``position=0`` keeps it out of the pending
    ordering, which is computed over pending rows only.

    Returns ``None`` when the printer has no queue row — the caller carries on
    and dispatches unclaimed, exactly as before this existed. A missing queue is
    a broken install, not a reason to refuse somebody's print.

    ⚠️ ``origin`` is REQUIRED and has no default, because this one helper builds
    the claim for two different things: ``"direct"`` for the Print dialog and
    ``"external"`` for a print BamDude never sent (``mark_queue_printing_for_printer``
    calls it when it finds no row). A default here would silently make
    one of them lie, and what it feeds — the queue-completed notifications — is
    exactly the question "did anybody queue this?". Never ``"queue"``: nothing
    reaching this function was queued.

    The caller owns the release: see ``background_dispatch``.
    """
    result = await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
    queue = result.scalar_one_or_none()
    if not queue:
        return None

    # The order named without its line — file the line when this plate points at
    # exactly one (spec pass 7, Decision 4a), the same rule and the same helper
    # the three queue writers use. ⚠️ "Print now" with quantity 1 never reaches
    # ``enqueue_batch_copies``, so without this the one door that dispatches
    # straight to a printer was also the one door that dropped the answer the
    # operator had just given the Print dialog's Order field. An explicit line is
    # never overridden; an ambiguous plate stays NULL and the plan's implicit
    # branch re-asks on every read.
    if project_id is not None and project_line_id is None:
        project_line_id = await resolve_line_id(
            db, project_id=project_id, library_file_id=library_file_id, plate_index=(options or {}).get("plate_id")
        )

    item = PrintQueueItem(
        queue_id=queue.id,
        position=0,
        status="printing",
        origin=origin,
        started_at=datetime.now(timezone.utc),
        created_by_id=created_by_id,
        **_item_columns(
            archive_id=archive_id,
            library_file_id=library_file_id,
            options=options,
            project_id=project_id,
            project_line_id=project_line_id,
        ),
    )
    db.add(item)
    await db.flush()

    await set_queue_printing(db, queue.id, item.id)
    await update_queue_counters(db, queue.id)
    await db.commit()
    await db.refresh(item)
    return item


async def enqueue_batch_copies(
    db: AsyncSession,
    *,
    printer_id: int,
    count: int,
    archive_id: int | None = None,
    library_file_id: int | None = None,
    plate_id: int | None = None,
    ams_mapping: list[int] | None = None,
    bed_levelling: str | bool = True,
    flow_cali: str | bool = True,
    layer_inspect: bool = False,
    timelapse: bool = False,
    timelapse_storage: str | None = None,
    use_ams: bool = True,
    nozzle_offset_cali: str | bool = True,
    mesh_mode_fast_check: bool = True,
    gcode_injection: bool = False,
    execute_swap_macros: bool = False,
    swap_macro_events: list[str] | None = None,
    selected_macro_ids: list[int] | None = None,
    auto_off_after: bool = False,
    created_by_id: int | None = None,
    project_id: int | None = None,
    project_line_id: int | None = None,
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

    # The order named without its line — file the line when this plate points at
    # exactly one (spec pass 7, Decision 4a). Every copy of a batch is the same
    # plate, so this is asked once for all of them. An explicit line is never
    # overridden; an ambiguous plate stays ``NULL`` and the plan's implicit
    # branch re-asks on every read.
    if project_id is not None and project_line_id is None:
        project_line_id = await resolve_line_id(
            db, project_id=project_id, library_file_id=library_file_id, plate_index=plate_id
        )

    if batch_id is None:
        batch_id = str(uuid.uuid4())
    # Tri-state calibration → canonical mode string (accepts legacy bool from
    # existing callers). Store the bool mirror + *_mode column on each copy.
    bed_mode = normalize_mode(bed_levelling)
    flow_mode = normalize_mode(flow_cali)
    nozzle_mode = normalize_mode(nozzle_offset_cali)
    ams_mapping_json = json.dumps(ams_mapping) if ams_mapping else None
    swap_macro_events_json = json.dumps(swap_macro_events) if execute_swap_macros and swap_macro_events else None
    selected_macro_ids_json = json.dumps(selected_macro_ids) if selected_macro_ids is not None else None

    items: list[PrintQueueItem] = []
    for i in range(count):
        items.append(
            PrintQueueItem(
                queue_id=queue.id,
                archive_id=archive_id,
                library_file_id=library_file_id,
                ams_mapping=ams_mapping_json,
                plate_id=plate_id,
                bed_levelling=bed_mode == "on",
                bed_levelling_mode=bed_mode,
                flow_cali=flow_mode == "on",
                flow_cali_mode=flow_mode,
                layer_inspect=layer_inspect,
                timelapse=timelapse,
                timelapse_storage=timelapse_storage,
                use_ams=use_ams,
                nozzle_offset_cali=nozzle_mode == "on",
                nozzle_offset_cali_mode=nozzle_mode,
                mesh_mode_fast_check=mesh_mode_fast_check,
                gcode_injection=gcode_injection,
                execute_swap_macros=execute_swap_macros,
                swap_macro_events=swap_macro_events_json,
                selected_macro_ids=selected_macro_ids_json,
                auto_off_after=auto_off_after,
                position=max_pos + 1 + i,
                status="pending",
                batch_id=batch_id,
                created_by_id=created_by_id,
                project_id=project_id,
                project_line_id=project_line_id,
            )
        )
    db.add_all(items)
    await db.commit()
    for it in items:
        await db.refresh(it)

    await update_queue_counters(db, queue.id)
    await db.commit()
    return items, batch_id
