"""API routes for the auto-queue layer.

CRUD over ``auto_queue_items`` plus batch and reorder helpers. Items
created here sit in a pre-dispatch staging area; the
AutoQueueScheduler (services/auto_queue_scheduler.py) periodically
picks them up and copies them into a per-printer print_queue when an
eligible idle printer is found.

Permissions reuse ``queue:*`` (per the project's RBAC catalogue):
- read     → QUEUE_READ
- create   → QUEUE_CREATE
- update   → QUEUE_UPDATE_OWN/ALL
- delete   → QUEUE_DELETE_OWN/ALL
- reorder  → QUEUE_REORDER
- batch ops → QUEUE_DELETE_ALL (mirrors print_queue batch endpoints)

See ``temp/auto-queue-adaptation-variants.md`` §12.7 for the full
endpoint contract.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.schemas.auto_queue import (
    AutoQueueBatchActionResponse,
    AutoQueueItemCreate,
    AutoQueueItemResponse,
    AutoQueueItemUpdate,
    AutoQueueReorder,
    AutoQueueStatsResponse,
)
from backend.app.services.auto_queue_eligibility import find_eligible_printer
from backend.app.services.auto_queue_threemf import extract_auto_queue_requirements

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auto-queue", tags=["auto-queue"])


def _resolve_source_paths(
    archive: PrintArchive | None,
    library_file: LibraryFile | None,
):
    """Return (path, print_time, default_target_model, default_filament_types).

    Used at create-time to auto-fill routing inputs from the source 3MF.
    """
    from pathlib import Path

    from backend.app.core.config import settings as app_settings

    if archive and archive.file_path:
        return app_settings.base_dir / archive.file_path
    if library_file and library_file.file_path:
        p = Path(library_file.file_path)
        return p if p.is_absolute() else app_settings.base_dir / library_file.file_path
    return None


def _to_response(item: AutoQueueItem) -> AutoQueueItemResponse:
    """Build an AutoQueueItemResponse from an ORM row, expanding JSON columns."""
    required_types = None
    if item.required_filament_types:
        try:
            required_types = json.loads(item.required_filament_types)
        except (ValueError, TypeError):
            required_types = None

    overrides = None
    if item.filament_overrides:
        try:
            overrides = json.loads(item.filament_overrides)
        except (ValueError, TypeError):
            overrides = None

    swap_events = None
    if item.swap_macro_events:
        try:
            swap_events = json.loads(item.swap_macro_events)
        except (ValueError, TypeError):
            swap_events = None

    response = AutoQueueItemResponse(
        id=item.id,
        archive_id=item.archive_id,
        library_file_id=item.library_file_id,
        project_id=item.project_id,
        target_model=item.target_model,
        target_location=item.target_location,
        required_filament_types=required_types,
        filament_overrides=overrides,
        force_color_match=item.force_color_match,
        plate_id=item.plate_id,
        position=item.position,
        scheduled_time=item.scheduled_time,
        manual_start=item.manual_start,
        auto_off_after=item.auto_off_after,
        require_previous_success=item.require_previous_success,
        bed_levelling=item.bed_levelling,
        flow_cali=item.flow_cali,
        layer_inspect=item.layer_inspect,
        timelapse=item.timelapse,
        use_ams=item.use_ams,
        mesh_mode_fast_check=item.mesh_mode_fast_check,
        execute_swap_macros=item.execute_swap_macros,
        swap_macro_events=swap_events,
        status=item.status,
        waiting_reason=item.waiting_reason,
        assigned_to_item_id=item.assigned_to_item_id,
        assigned_at=item.assigned_at,
        cancelled_at=item.cancelled_at,
        print_time_seconds=item.print_time_seconds,
        been_jumped=item.been_jumped,
        batch_id=item.batch_id,
        created_at=item.created_at,
        created_by_id=item.created_by_id,
    )

    # UI-friendly nested data. Both ``PrintArchive`` and ``LibraryFile`` store
    # the bare on-disk filename in a column literally named ``filename`` —
    # the ``original_filename`` accessor used by the original auto-queue feature
    # commit (81ae73f) referred to a column that was never added, so every POST
    # that reached this builder raised ``AttributeError`` and the route returned
    # 500 to the client *after* ``db.commit()`` had already persisted the
    # auto-queue rows. Operators saw rows show up but the dispatch toast read
    # "Internal Server Error", and frontend retries duplicated the items
    # (support bundle 2026-05-04). Mirror ``print_queue._to_response`` here:
    # archives get ``print_name or filename`` (so multi-plate suffix surfaces),
    # library files prefer the parsed ``file_metadata['print_name']`` and fall
    # back to the bare filename.
    if item.archive is not None:
        response.archive_name = item.archive.print_name or item.archive.filename
        response.archive_thumbnail = item.archive.thumbnail_path
    if item.library_file is not None:
        meta = item.library_file.file_metadata if item.library_file.file_metadata else None
        response.library_file_name = (meta.get("print_name") if meta else None) or item.library_file.filename
        response.library_file_thumbnail = item.library_file.thumbnail_path
    if item.created_by is not None:
        response.created_by_username = item.created_by.username
    if item.assigned_to is not None:
        # We need the printer through queue → printer; populate for UI
        # (the calling endpoint loads this with selectinload chain).
        try:
            queue = item.assigned_to.queue
            if queue and queue.printer is not None:
                response.assigned_printer_id = queue.printer.id
                response.assigned_printer_name = queue.printer.name
        except Exception:
            pass

    return response


@router.post("/", response_model=AutoQueueItemResponse)
async def add_to_auto_queue(
    data: AutoQueueItemCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
):
    """Add one or more items to the auto-queue.

    Behavior:
    - Either ``archive_id`` or ``library_file_id`` is required.
    - When ``target_model`` / ``required_filament_types`` are omitted,
      they're auto-extracted from the source 3MF (slice_info.config).
    - ``quantity > 1`` creates N items sharing a UUID ``batch_id``,
      sequential ``position`` starting at max+1.
    - ``plate_ids`` (multi-plate) creates one item per plate.
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
        # m044: eager-load M2M projects so the inherit-fallback below
        # doesn't lazy-fetch.
        result = await db.execute(
            LibraryFile.active()
            .options(selectinload(LibraryFile.projects))
            .where(LibraryFile.id == data.library_file_id)
        )
        library_file = result.scalar_one_or_none()
        if not library_file:
            raise HTTPException(400, "Library file not found")

    if data.project_id is not None:
        result = await db.execute(select(Project).where(Project.id == data.project_id))
        if not result.scalar_one_or_none():
            raise HTTPException(404, "Project not found")

    # Inherit project from library file if not set explicitly. m044:
    # multi-project file → first project as fallback (auto-queue items
    # are single-project by design; operator passes ``project_id`` to
    # disambiguate).
    effective_project_id = data.project_id
    if effective_project_id is None and library_file is not None and library_file.projects:
        effective_project_id = library_file.projects[0].id

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
    file_path = _resolve_source_paths(archive, library_file)

    # Compute next position (auto-queue is global, single ordering)
    max_pos_q = await db.execute(
        select(func.coalesce(func.max(AutoQueueItem.position), 0)).where(AutoQueueItem.status == "pending")
    )
    max_pos = int(max_pos_q.scalar() or 0)

    batch_id = str(uuid.uuid4()) if (data.quantity > 1 or len(plate_ids) > 1) else None

    overrides_json = None
    if data.filament_overrides:
        overrides_json = json.dumps([o.model_dump() for o in data.filament_overrides])
    swap_events_json = json.dumps(data.swap_macro_events) if data.swap_macro_events else None

    items: list[AutoQueueItem] = []
    pos_offset = 0
    for plate_id in plate_ids:
        # Per-plate 3MF auto-extraction (fall back to provided values when given)
        target_model = data.target_model
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

        for _ in range(data.quantity):
            pos_offset += 1
            items.append(
                AutoQueueItem(
                    archive_id=data.archive_id,
                    library_file_id=data.library_file_id,
                    project_id=effective_project_id,
                    target_model=target_model,
                    target_location=data.target_location,
                    required_filament_types=required_types_json,
                    filament_overrides=overrides_json,
                    force_color_match=data.force_color_match,
                    plate_id=plate_id,
                    bed_levelling=data.bed_levelling,
                    flow_cali=data.flow_cali,
                    layer_inspect=data.layer_inspect,
                    timelapse=data.timelapse,
                    use_ams=data.use_ams,
                    mesh_mode_fast_check=data.mesh_mode_fast_check,
                    execute_swap_macros=data.execute_swap_macros,
                    swap_macro_events=swap_events_json,
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

    # Re-load first item with eager relationships for the response
    first = await db.execute(
        select(AutoQueueItem)
        .options(
            selectinload(AutoQueueItem.archive),
            selectinload(AutoQueueItem.library_file),
            selectinload(AutoQueueItem.created_by),
        )
        .where(AutoQueueItem.id == items[0].id)
    )
    return _to_response(first.scalar_one())


@router.get("/", response_model=list[AutoQueueItemResponse])
async def list_auto_queue(
    status_filter: str | None = Query(None, alias="status"),
    batch_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """List auto-queue items, optionally filtered by status / batch_id."""
    stmt = select(AutoQueueItem).options(
        selectinload(AutoQueueItem.archive),
        selectinload(AutoQueueItem.library_file),
        selectinload(AutoQueueItem.created_by),
        selectinload(AutoQueueItem.assigned_to).selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
    )
    if status_filter:
        stmt = stmt.where(AutoQueueItem.status == status_filter)
    if batch_id:
        stmt = stmt.where(AutoQueueItem.batch_id == batch_id)
    stmt = stmt.order_by(AutoQueueItem.position)
    result = await db.execute(stmt)
    return [_to_response(item) for item in result.scalars().all()]


@router.get("/stats", response_model=AutoQueueStatsResponse)
async def auto_queue_stats(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """Archive-backed totals for auto-queue dispatched prints.

    The ``auto_queue_items`` row is deleted once its print finishes
    (it's a pre-dispatch router), so the lasting record is
    ``print_archives.from_auto_queue``. Mirrors the per-printer queue
    card footer: ``cancelled`` folds in ``aborted`` / ``stopped``.

    Declared before ``/{item_id}`` so the literal path wins over the
    int path-param.
    """
    result = await db.execute(
        select(PrintArchive.status, func.count())
        .where(PrintArchive.from_auto_queue.is_(True))
        .group_by(PrintArchive.status)
    )
    by_status = {row[0]: int(row[1] or 0) for row in result.all()}
    cancelled = by_status.get("cancelled", 0) + by_status.get("aborted", 0) + by_status.get("stopped", 0)
    return AutoQueueStatsResponse(
        completed_count=by_status.get("completed", 0),
        failed_count=by_status.get("failed", 0),
        cancelled_count=cancelled,
        total_count=sum(by_status.values()),
    )


@router.get("/{item_id}", response_model=AutoQueueItemResponse)
async def get_auto_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    result = await db.execute(
        select(AutoQueueItem)
        .options(
            selectinload(AutoQueueItem.archive),
            selectinload(AutoQueueItem.library_file),
            selectinload(AutoQueueItem.created_by),
            selectinload(AutoQueueItem.assigned_to)
            .selectinload(PrintQueueItem.queue)
            .selectinload(PrinterQueue.printer),
        )
        .where(AutoQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Auto-queue item not found")
    return _to_response(item)


@router.put("/{item_id}", response_model=AutoQueueItemResponse)
async def update_auto_queue_item(
    item_id: int,
    data: AutoQueueItemUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Update a pending auto-queue item.

    Once assigned, the per-printer print_queue item is the source of
    truth — edit there via ``PATCH /queue/{id}``.
    """
    result = await db.execute(select(AutoQueueItem).where(AutoQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Auto-queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Cannot edit item in status '{item.status}'")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if key == "filament_overrides" and value is not None:
            value = json.dumps([o if isinstance(o, dict) else o.model_dump() for o in value])
        elif key == "required_filament_types" and value is not None or key == "swap_macro_events" and value is not None:
            value = json.dumps(value)
        setattr(item, key, value)

    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.delete("/{item_id}", response_model=AutoQueueItemResponse)
async def cancel_auto_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_DELETE_ALL),
):
    """Cancel an auto-queue item.

    - status='pending' → mark cancelled in auto_queue_items only.
    - status='assigned' → also cancel the linked per-printer item if
      it's still pending (printing items use the existing per-printer
      stop endpoint).
    """
    result = await db.execute(select(AutoQueueItem).where(AutoQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Auto-queue item not found")
    if item.status == "cancelled":
        return _to_response(item)

    item.status = "cancelled"
    item.cancelled_at = datetime.now(timezone.utc)

    if item.assigned_to_item_id:
        pq_result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item.assigned_to_item_id))
        pq_item = pq_result.scalar_one_or_none()
        if pq_item and pq_item.status == "pending":
            pq_item.status = "cancelled"

    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.post("/reorder")
async def reorder_auto_queue(
    payload: AutoQueueReorder,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_REORDER),
):
    """Persist a new ordering of pending auto items."""
    for entry in payload.items:
        await db.execute(
            select(AutoQueueItem).where(AutoQueueItem.id == entry.id).execution_options(synchronize_session=False)
        )
    # Use ORM update for clarity / per-row event firing
    for entry in payload.items:
        result = await db.execute(select(AutoQueueItem).where(AutoQueueItem.id == entry.id))
        row = result.scalar_one_or_none()
        if row is not None and row.status == "pending":
            row.position = entry.position
    await db.commit()
    return {"reordered": len(payload.items)}


@router.post("/{item_id}/assign-now", response_model=AutoQueueItemResponse)
async def assign_now(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_REORDER),
):
    """Force an immediate assignment attempt for a single item.

    Bypasses ``manual_start`` and ``scheduled_time``. Returns the
    item as-is if no eligible printer is available; otherwise the
    item is assigned via the same ``_assign`` path the scheduler uses.
    """
    from backend.app.services.auto_queue_scheduler import auto_queue_scheduler

    result = await db.execute(select(AutoQueueItem).where(AutoQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Auto-queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Cannot assign-now item in status '{item.status}'")

    busy_result = await db.execute(select(PrinterQueue.printer_id).where(PrinterQueue.status == "printing"))
    busy_printers: set[int] = {pid for (pid,) in busy_result.all()}

    printer, reason = await find_eligible_printer(db, item, busy_printers)
    if printer is None:
        if reason:
            item.waiting_reason = reason
            await db.commit()
        raise HTTPException(409, reason or "No eligible printer available")

    await auto_queue_scheduler._assign(db, item, printer)
    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.delete("/batch/{batch_id}", response_model=AutoQueueBatchActionResponse)
async def cancel_auto_queue_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_DELETE_ALL),
):
    """Cancel all pending items of a batch (mirrors ``POST /queue/batch/{id}/cancel``)."""
    pending_result = await db.execute(
        select(AutoQueueItem).where(
            AutoQueueItem.batch_id == batch_id,
            AutoQueueItem.status == "pending",
        )
    )
    pending = list(pending_result.scalars().all())
    now = datetime.now(timezone.utc)
    for item in pending:
        item.status = "cancelled"
        item.cancelled_at = now

    # Also cancel any per-printer items dispatched from these auto items
    # that are still pending
    if pending:
        assigned_ids = [it.assigned_to_item_id for it in pending if it.assigned_to_item_id is not None]
        # Pending in this batch shouldn't have assigned_to set, but defensive:
        if assigned_ids:
            pq_result = await db.execute(
                select(PrintQueueItem).where(
                    PrintQueueItem.id.in_(assigned_ids),
                    PrintQueueItem.status == "pending",
                )
            )
            for pq_item in pq_result.scalars().all():
                pq_item.status = "cancelled"

    # Also cancel assigned-but-pending items linked via source_auto_item_id
    auto_in_batch_ids_result = await db.execute(select(AutoQueueItem.id).where(AutoQueueItem.batch_id == batch_id))
    auto_ids = [row[0] for row in auto_in_batch_ids_result.all()]
    if auto_ids:
        pq_pending_result = await db.execute(
            select(PrintQueueItem).where(
                PrintQueueItem.source_auto_item_id.in_(auto_ids),
                PrintQueueItem.status == "pending",
            )
        )
        for pq_item in pq_pending_result.scalars().all():
            pq_item.status = "cancelled"

    await db.commit()
    return AutoQueueBatchActionResponse(affected=len(pending), batch_id=batch_id)


# Avoid unused-import warnings — these are referenced via type hints / FK strings
_ = Printer
_ = or_
