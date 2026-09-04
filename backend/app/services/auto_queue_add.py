"""Creating auto-queue items — one definition.

Every gate and every fan-out rule lives here: the source must exist and not be
trashed, an order line must belong to the order named beside it, a multi-plate
request becomes one row per plate, and ``quantity`` (or ``plate_quantities``)
becomes N rows sharing a ``batch_id``.

Extracted when the order plan needed the same writer (spec pass 3,
``POST /projects/{id}/plan/enqueue``). The shape mirrors
``services/queue_add.py::add_items_to_printer_queue``, which was extracted from
``POST /queue/`` for the same reason: a second caller that re-implemented the
fan-out would drift, and the drift would surface as rows the scheduler cannot
route rather than as an error somebody sees.

``HTTPException`` is the raised form because ``POST /auto-queue/`` re-raises it
unchanged.

⚠️ This function COMMITS. The auto-queue route has always committed inside the
handler (unlike the orders routes, which leave it to ``get_db``), and the plan's
enqueue endpoint therefore commits once per item — see its docstring.
"""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.calibration_mode import mode_to_bool
from backend.app.services.auto_queue_threemf import extract_auto_queue_requirements
from backend.app.services.filament_requirements import overrides_for_plate
from backend.app.utils.printer_models import normalize_model_name


def _resolve_source_path(archive: PrintArchive | None, library_file: LibraryFile | None):
    """The 3MF on disk behind the request, or ``None``.

    Used at create-time to auto-fill routing inputs (target model, filament
    types, print time) from the source file.
    """
    from pathlib import Path

    from backend.app.core.config import settings as app_settings

    if archive and archive.file_path:
        return app_settings.base_dir / archive.file_path
    if library_file and library_file.file_path:
        p = Path(library_file.file_path)
        return p if p.is_absolute() else app_settings.base_dir / library_file.file_path
    return None


async def add_items_to_auto_queue(
    db: AsyncSession,
    data: AutoQueueItemCreate,
    current_user: User | None,
) -> list[AutoQueueItem]:
    """Validate, build and persist the auto-queue rows ``data`` asks for.

    Returns the created items in position order. Commits — see the module
    docstring.
    """
    if not data.archive_id and not data.library_file_id:
        raise HTTPException(400, "Either archive_id or library_file_id must be provided")

    archive = None
    if data.archive_id:
        result = await db.execute(select(PrintArchive).where(PrintArchive.id == data.archive_id))
        archive = result.scalar_one_or_none()
        if not archive:
            raise HTTPException(400, "Archive not found")

    library_file = None
    if data.library_file_id:
        # Trash bin (#1008): refuse to dispatch a soft-deleted source.
        result = await db.execute(LibraryFile.active().where(LibraryFile.id == data.library_file_id))
        library_file = result.scalar_one_or_none()
        if not library_file:
            raise HTTPException(400, "Library file not found")

    if data.project_id is not None:
        result = await db.execute(select(Project).where(Project.id == data.project_id))
        if not result.scalar_one_or_none():
            raise HTTPException(404, "Project not found")

    # A file does not belong to an order, so there is nothing to fall back on:
    # the caller names the order, or the row carries none.
    effective_project_id = data.project_id

    # The order LINE, by the same rule the queue and direct-print doors apply:
    # it must be a line of the order named beside it (else 404, not a
    # FK-constraint 500), and naming only the line derives the order.
    if data.project_line_id is not None:
        line = await db.get(ProjectLine, data.project_line_id)
        if line is None or (data.project_id is not None and line.project_id != data.project_id):
            raise HTTPException(404, "Order line not found in this project")
        effective_project_id = line.project_id

    # Resolve plate IDs to fan out (one row per plate)
    plate_ids: list[int | None]
    if data.plate_ids:
        plate_ids = list(data.plate_ids)
    elif data.plate_id is not None:
        plate_ids = [data.plate_id]
    else:
        plate_ids = [None]

    # Auto-extract target_model + required_filament_types + print_time from 3MF
    # when not explicitly provided. Done per-plate so multi-plate items get
    # accurate per-plate info.
    file_path = _resolve_source_path(archive, library_file)

    # Compute next position (auto-queue is global, single ordering)
    max_pos_q = await db.execute(
        select(func.coalesce(func.max(AutoQueueItem.position), 0)).where(AutoQueueItem.status == "pending")
    )
    max_pos = int(max_pos_q.scalar() or 0)

    # How many runs each plate was asked for. Absent for a plate → the shared
    # ``quantity``, which is what every caller sent before per-plate counts
    # existed.
    per_plate = data.plate_quantities or {}

    def _quantity_for(plate: int | None) -> int:
        return per_plate.get(plate, data.quantity) if plate is not None else data.quantity

    total_items = sum(_quantity_for(p) for p in plate_ids)
    # A batch is "these rows were created together", so it is the TOTAL that
    # decides — two plates at one copy each is still a batch, and one plate at
    # three is too.
    batch_id = str(uuid.uuid4()) if total_items > 1 else None

    # Raw override dicts; narrowed per-plate inside the loop below (#2551).
    overrides_list = [o.model_dump() for o in data.filament_overrides] if data.filament_overrides else []
    swap_events_json = json.dumps(data.swap_macro_events) if data.swap_macro_events else None
    selected_macros_json = json.dumps(data.selected_macro_ids) if data.selected_macro_ids is not None else None

    items: list[AutoQueueItem] = []
    pos_offset = 0
    for plate_id in plate_ids:
        # Per-plate 3MF auto-extraction (fall back to provided values when given)
        # Normalised on the way in so the stored value is the short name the
        # rest of the app compares and displays. Routing normalises again when
        # it reads (that is what covers rows written by telegram and the VP),
        # but a row that keeps "C12" shows "C12" everywhere it is named.
        target_model = normalize_model_name(data.target_model)
        required_types = data.required_filament_types
        print_time = None
        if file_path is not None and file_path.exists():
            reqs = extract_auto_queue_requirements(file_path, plate_id=plate_id)
            if not target_model and reqs.target_model:
                target_model = reqs.target_model
            if required_types is None and reqs.required_filament_types:
                required_types = reqs.required_filament_types
            print_time = reqs.print_time_seconds

        required_types_json = json.dumps(required_types) if required_types is not None else None

        # Narrow force-colour overrides to the slots THIS plate prints (#2551) —
        # otherwise a single-colour plate waits on every colour in the batch.
        plate_overrides = overrides_for_plate(overrides_list, file_path, plate_id)
        plate_overrides_json = json.dumps(plate_overrides) if plate_overrides else None

        for _ in range(_quantity_for(plate_id)):
            pos_offset += 1
            items.append(
                AutoQueueItem(
                    archive_id=data.archive_id,
                    library_file_id=data.library_file_id,
                    project_id=effective_project_id,
                    project_line_id=data.project_line_id,
                    target_model=target_model,
                    target_location_id=data.target_location_id,
                    required_filament_types=required_types_json,
                    filament_overrides=plate_overrides_json,
                    force_color_match=data.force_color_match,
                    plate_id=plate_id,
                    bed_levelling=mode_to_bool(data.bed_levelling),
                    flow_cali=mode_to_bool(data.flow_cali),
                    layer_inspect=data.layer_inspect,
                    timelapse=data.timelapse,
                    timelapse_storage=data.timelapse_storage,
                    use_ams=data.use_ams,
                    mesh_mode_fast_check=data.mesh_mode_fast_check,
                    execute_swap_macros=data.execute_swap_macros,
                    swap_macro_events=swap_events_json,
                    selected_macro_ids=selected_macros_json,
                    position=max_pos + pos_offset,
                    scheduled_time=data.scheduled_time,
                    manual_start=data.manual_start,
                    auto_off_after=data.auto_off_after,
                    require_previous_success=data.require_previous_success,
                    status="pending",
                    print_time_seconds=print_time,
                    batch_id=batch_id,
                    created_by_id=current_user.id if current_user else None,
                )
            )

    db.add_all(items)
    await db.commit()
    for it in items:
        await db.refresh(it)
    return items
