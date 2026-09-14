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
from backend.app.models.queue_source import QueueSource
from backend.app.models.user import User
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.calibration_mode import mode_to_bool
from backend.app.services import queue_sources
from backend.app.services.filament_intake import routing_detail
from backend.app.services.filament_policy import feed_policy
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.order_filing import line_filer
from backend.app.services.queue_source_capture import (
    StagedSource,
    capture_staged,
    discard_staged,
    plan_capture,
    publish_staged,
    staged_requirements,
)
from backend.app.utils.printer_models import normalize_model_name


async def add_items_to_auto_queue(
    db: AsyncSession,
    data: AutoQueueItemCreate,
    current_user: User | None,
    *,
    requirements_cache: PrintRequirementsCache | None = None,
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

    # ⚠️ ONE capture for the whole request — spec §5's fan-out rule (A01): a
    # multi-plate request with a quantity per plate is still one walk over the
    # share, and the per-plate reads below all happen on the local copy. The
    # request's transaction is released first, because a copy can take minutes
    # and this function committed at the end anyway (see the module docstring);
    # the commit only moves earlier. Nothing below the commit reaches through an
    # ORM relationship: the two source rows are read for their own columns only
    # (both session factories are ``expire_on_commit=False``), and every write
    # happens in the publication's session.
    plan = plan_capture(archive=archive, library_file=library_file)
    await db.commit()
    staged = await capture_staged(plan)
    try:
        # Auto-extract target_model + required_filament_types + print_time from
        # the CAPTURED 3MF when not explicitly provided (§5 step 4). Done
        # per-plate so multi-plate items get accurate per-plate info — and a
        # request naming a plate the file does not have is refused right here,
        # with the staged copy in hand and nothing published.
        cache = requirements_cache or PrintRequirementsCache()
        resolved = [
            (plate, await staged_requirements(staged, cache, archive, library_file, plate)) for plate in plate_ids
        ]
        used_slots = {f["slot_id"] for _, req in resolved for f in req.used_filaments}
        if any(o.slot_id not in used_slots for o in data.filament_overrides or []):
            raise HTTPException(422, routing_detail("override_slot_not_used"))
        created_ids = await _publish_items(data, staged, resolved, effective_project_id, current_user, library_file)
    except BaseException:
        await discard_staged(staged)
        raise

    # Re-read in the CALLER's session: the rows were written by the publication's
    # own transaction (§5 step 6), and every caller goes on to use them.
    return list(
        (
            await db.execute(
                select(AutoQueueItem).where(AutoQueueItem.id.in_(created_ids)).order_by(AutoQueueItem.position)
            )
        )
        .scalars()
        .all()
    )


async def _publish_items(
    data: AutoQueueItemCreate,
    staged: StagedSource,
    resolved: list,
    effective_project_id: int | None,
    current_user: User | None,
    library_file: LibraryFile | None,
) -> list[int]:
    """Publish the captured bytes and write every row in ONE transaction (§5 step 6).

    The fan-out lives here, inside ``publish``'s own transaction, so the blob's
    row and every job row that names it are committed together — after the file
    is in place. The 3MF was already read by the caller: the storage guard this
    runs under is process-wide, and a ZIP parse under it would serialise every
    other publication behind this one.
    """
    # How many runs each plate was asked for. Absent for a plate → the shared
    # ``quantity``, which is what every caller sent before per-plate counts
    # existed.
    per_plate = data.plate_quantities or {}

    def _quantity_for(plate: int | None) -> int:
        return per_plate.get(plate, data.quantity) if plate is not None else data.quantity

    total_items = sum(_quantity_for(plate) for plate, _ in resolved)
    # A batch is "these rows were created together", so it is the TOTAL that
    # decides — two plates at one copy each is still a batch, and one plate at
    # three is too.
    batch_id = str(uuid.uuid4()) if total_items > 1 else None

    # Raw override dicts; narrowed per-plate inside the loop below (#2551).
    overrides_list = [o.model_dump() for o in data.filament_overrides] if data.filament_overrides else []
    swap_events_json = json.dumps(data.swap_macro_events) if data.swap_macro_events else None
    selected_macros_json = json.dumps(data.selected_macro_ids) if data.selected_macro_ids is not None else None

    created_ids: list[int] = []

    async def attach(session: AsyncSession, source: QueueSource) -> None:
        # Compute next position (auto-queue is global, single ordering) — read in
        # the same transaction as the inserts it feeds.
        max_pos_q = await session.execute(
            select(func.coalesce(func.max(AutoQueueItem.position), 0)).where(AutoQueueItem.status == "pending")
        )
        max_pos = int(max_pos_q.scalar() or 0)

        # What filing this file under this order needs, loaded ONCE for the
        # request: the order's lines, the library file and its product plates
        # depend on the order and the file, never on the plate index. The question
        # below is per plate, but the rows it reads are not, and asking
        # ``resolve_line_id`` inside the fan-out repeated all three SELECTs per
        # plate of a multi-plate call. ``file=library_file``: validated at the top
        # of the caller, so the filer does not SELECT the same row a second time.
        filer = (
            await line_filer(
                session, project_id=effective_project_id, library_file_id=data.library_file_id, file=library_file
            )
            if effective_project_id is not None and data.project_line_id is None
            else None
        )

        items: list[AutoQueueItem] = []
        pos_offset = 0
        for requested_plate_id, reqs in resolved:
            plate_id = reqs.resolved_plate_id
            # The order without the line — file it ourselves when this plate
            # points at exactly one (spec pass 7, Decision 4a). ⚠️ **Per plate,
            # inside the fan-out**, not once for the request: a multi-plate 3MF
            # can hold two products' plates, so plate 1 and plate 2 of one call
            # legitimately land on two different lines. An explicit line is never
            # overridden, and an ambiguous plate is left ``NULL`` for the plan's
            # implicit branch.
            plate_line_id = data.project_line_id
            if plate_line_id is None and filer is not None:
                plate_line_id = filer.for_plate(plate_id)
            # Per-plate 3MF auto-extraction (fall back to provided values when
            # given). Normalised on the way in so the stored value is the short
            # name the rest of the app compares and displays. Routing normalises
            # again when it reads (that is what covers rows written by telegram
            # and the VP), but a row that keeps "C12" shows "C12" everywhere it
            # is named.
            target_model = normalize_model_name(data.target_model)
            required_types = data.required_filament_types
            print_time = reqs.print_time_seconds
            if not target_model:
                target_model = reqs.model
            if required_types is None:
                required_types = list(dict.fromkeys(f["type"] for f in reqs.used_filaments))

            required_types_json = json.dumps(required_types) if required_types is not None else None

            # Narrow force-colour overrides to the slots THIS plate prints
            # (#2551) — otherwise a single-colour plate waits on every colour in
            # the batch.
            used_slots = {f["slot_id"] for f in reqs.used_filaments}
            plate_overrides = [o for o in overrides_list if o["slot_id"] in used_slots]
            plate_overrides_json = json.dumps(plate_overrides) if plate_overrides else None

            for _ in range(_quantity_for(requested_plate_id)):
                pos_offset += 1
                items.append(
                    AutoQueueItem(
                        queue_source_id=source.id,
                        source_snapshot=queue_sources.snapshot_for(staged.receipt, source),
                        archive_id=data.archive_id,
                        library_file_id=data.library_file_id,
                        project_id=effective_project_id,
                        project_line_id=plate_line_id,
                        target_model=target_model,
                        target_location_id=data.target_location_id,
                        required_filament_types=required_types_json,
                        filament_overrides=plate_overrides_json,
                        force_color_match=data.force_color_match,
                        allow_base_material_match=data.allow_base_material_match,
                        plate_id=plate_id,
                        bed_levelling=mode_to_bool(data.bed_levelling),
                        flow_cali=mode_to_bool(data.flow_cali),
                        layer_inspect=data.layer_inspect,
                        timelapse=data.timelapse,
                        timelapse_storage=data.timelapse_storage,
                        feed_policy=feed_policy(data.feed_policy, data.use_ams),
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

        session.add_all(items)
        await session.flush()
        created_ids.extend(item.id for item in items)

    await publish_staged(staged, attach)
    return created_ids
