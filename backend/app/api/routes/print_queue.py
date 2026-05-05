"""API routes for print queue management."""

import json
import logging
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import defusedxml.ElementTree as ET
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermission, require_ownership_permission
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.schemas.print_queue import (
    PrintQueueBulkUpdate,
    PrintQueueBulkUpdateResponse,
    PrintQueueItemCreate,
    PrintQueueItemResponse,
    PrintQueueItemUpdate,
    PrintQueueReorder,
)
from backend.app.services.notification_service import notification_service
from backend.app.utils.threemf_tools import extract_filament_usage_from_3mf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"])


def _extract_filament_types_from_3mf(file_path: Path, plate_id: int | None = None) -> list[str]:
    """Extract unique filament types from a 3MF file.

    Args:
        file_path: Path to the 3MF file
        plate_id: Optional plate index to filter for (for multi-plate files)

    Returns:
        List of unique filament types (e.g., ["PLA", "PETG"])
    """
    types: set[str] = set()

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return []

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            if plate_id is not None:
                # Find the plate element with matching index
                for plate_elem in root.findall(".//plate"):
                    plate_index = None
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                plate_index = int(meta.get("value", "0"))
                            except ValueError:
                                pass  # Skip plate with unparseable index
                            break

                    if plate_index == plate_id:
                        for filament_elem in plate_elem.findall("filament"):
                            filament_type = filament_elem.get("type", "")
                            used_g = filament_elem.get("used_g", "0")
                            try:
                                used_grams = float(used_g)
                            except (ValueError, TypeError):
                                used_grams = 0
                            if used_grams > 0 and filament_type:
                                types.add(filament_type)
                        break
            else:
                # No plate_id specified - extract all filaments with used_g > 0
                for filament_elem in root.findall(".//filament"):
                    filament_type = filament_elem.get("type", "")
                    used_g = filament_elem.get("used_g", "0")
                    try:
                        used_grams = float(used_g)
                    except (ValueError, TypeError):
                        used_grams = 0
                    if used_grams > 0 and filament_type:
                        types.add(filament_type)

    except Exception as e:
        logger.warning("Failed to extract filament types from %s: %s", file_path, e)

    return sorted(types)


def _extract_print_time_from_3mf(file_path: Path, plate_id: int | None = None) -> int | None:
    """Extract print time (prediction) from a 3MF file.

    Args:
        file_path: Path to the 3MF file
        plate_id: Optional plate index to filter for (for multi-plate files)

    Returns:
        Print time in seconds, or None if not found
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            if plate_id is not None:
                for plate_elem in root.findall(".//plate"):
                    plate_index = None
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                plate_index = int(meta.get("value", "0"))
                            except ValueError:
                                pass  # Skip plate with unparseable index
                            break

                    if plate_index == plate_id:
                        for meta in plate_elem.findall("metadata"):
                            if meta.get("key") == "prediction":
                                try:
                                    return int(meta.get("value", "0"))
                                except ValueError:
                                    return None
                        break
            else:
                plate_elem = root.find(".//plate")
                if plate_elem is not None:
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "prediction":
                            try:
                                return int(meta.get("value", "0"))
                            except ValueError:
                                return None
    except Exception as e:
        logger.warning("Failed to extract print time from %s: %s", file_path, e)

    return None


def _enrich_response(item: PrintQueueItem) -> PrintQueueItemResponse:
    """Add nested archive/printer/library_file info to response."""
    # Parse ams_mapping from JSON string BEFORE model_validate
    ams_mapping_parsed = None
    if item.ams_mapping:
        try:
            ams_mapping_parsed = json.loads(item.ams_mapping)
        except json.JSONDecodeError:
            ams_mapping_parsed = None

    # Create response with parsed ams_mapping
    item_dict = {
        "id": item.id,
        "queue_id": item.queue_id,
        "printer_id": item.printer_id,  # convenience property from queue
        "project_id": item.project_id,
        "waiting_reason": item.waiting_reason,
        "archive_id": item.archive_id,
        "library_file_id": item.library_file_id,
        "position": item.position,
        "scheduled_time": item.scheduled_time,
        "auto_off_after": item.auto_off_after,
        "manual_start": item.manual_start,
        "ams_mapping": ams_mapping_parsed,
        "plate_id": item.plate_id,
        "bed_levelling": item.bed_levelling,
        "flow_cali": item.flow_cali,
        "layer_inspect": item.layer_inspect,
        "timelapse": item.timelapse,
        "use_ams": item.use_ams,
        "mesh_mode_fast_check": item.mesh_mode_fast_check,
        "execute_swap_macros": item.execute_swap_macros,
        "swap_macro_events": json.loads(item.swap_macro_events) if item.swap_macro_events else None,
        "gcode_injection": item.gcode_injection,
        "status": item.status,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "batch_id": item.batch_id,
        # User tracking (Issue #206)
        "created_by_id": item.created_by_id,
        "created_by_username": item.created_by.username if item.created_by else None,
    }
    response = PrintQueueItemResponse(**item_dict)
    if item.archive:
        response.archive_name = item.archive.print_name or item.archive.filename
        response.archive_thumbnail = item.archive.thumbnail_path
        response.print_time_seconds = item.archive.print_time_seconds
        response.filament_used_grams = item.archive.filament_used_grams
        response.filament_type = item.archive.filament_type
        response.filament_color = item.archive.filament_color
        response.layer_height = item.archive.layer_height
        response.nozzle_diameter = item.archive.nozzle_diameter
        response.sliced_for_model = item.archive.sliced_for_model
        if item.plate_id:
            archive_path = settings.base_dir / item.archive.file_path
            if archive_path.exists():
                plate_time = _extract_print_time_from_3mf(archive_path, item.plate_id)
                plate_weight = sum(f["used_g"] for f in extract_filament_usage_from_3mf(archive_path, item.plate_id))
                if plate_time is not None:
                    response.print_time_seconds = plate_time
                if plate_weight > 0:
                    response.filament_used_grams = plate_weight
    if item.library_file:
        response.library_file_name = (
            item.library_file.file_metadata.get("print_name") if item.library_file.file_metadata else None
        )
        if not response.library_file_name:
            response.library_file_name = item.library_file.filename
        response.library_file_thumbnail = item.library_file.thumbnail_path
        # Get metadata from library file if no archive
        if not item.archive and item.library_file.file_metadata:
            response.print_time_seconds = item.library_file.file_metadata.get("print_time_seconds")
            response.filament_used_grams = item.library_file.file_metadata.get("filament_used_grams")
            response.filament_type = item.library_file.file_metadata.get("filament_type")
            response.filament_color = item.library_file.file_metadata.get("filament_color")
            response.layer_height = item.library_file.file_metadata.get("layer_height")
            response.nozzle_diameter = item.library_file.file_metadata.get("nozzle_diameter")
            response.sliced_for_model = item.library_file.file_metadata.get("sliced_for_model")
        if item.plate_id:
            lib_path = Path(item.library_file.file_path)
            library_file_path = lib_path if lib_path.is_absolute() else settings.base_dir / item.library_file.file_path
            if library_file_path.exists():
                plate_time = _extract_print_time_from_3mf(library_file_path, item.plate_id)
                plate_weight = sum(
                    f["used_g"] for f in extract_filament_usage_from_3mf(library_file_path, item.plate_id)
                )
                if plate_time is not None:
                    response.print_time_seconds = plate_time
                if plate_weight > 0:
                    response.filament_used_grams = plate_weight
    if item.queue and item.queue.printer:
        response.printer_name = item.queue.printer.name
    return response


@router.get("/stagger-state")
async def get_stagger_state(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """Current stagger slot occupancy for the UI diagnostic banner."""
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    return await print_scheduler.get_stagger_state_snapshot(db)


@router.get("/", response_model=list[PrintQueueItemResponse])
async def list_queue(
    queue_id: int | None = Query(None, description="Filter by printer queue"),
    status: str | None = Query(None, description="Filter by status"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """List all queue items, optionally filtered by queue or status."""
    query = (
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .order_by(PrintQueueItem.queue_id, PrintQueueItem.position)
    )

    if queue_id is not None:
        query = query.where(PrintQueueItem.queue_id == queue_id)
    if status:
        query = query.where(PrintQueueItem.status == status)

    result = await db.execute(query)
    items = result.scalars().all()
    enriched = [_enrich_response(item) for item in items]

    # Augment with virtual current-print items for printers whose queue
    # doesn't have a printing item but whose printer is actively busy
    # (external / direct-dispatch prints).  Skipped when the caller
    # filtered by a specific non-matching status.
    if not status or status == "printing":
        from backend.app.services.queue_virtual import build_virtual_current_print

        # Find which queue ids to scan — either the requested one or all
        # queues that showed up in the result set, plus queues that had
        # no items at all (need a separate query for those).
        if queue_id is not None:
            target_queue_ids = [queue_id]
        else:
            all_queues = (await db.execute(select(PrinterQueue))).scalars().all()
            target_queue_ids = [q.id for q in all_queues]

        for q_id in target_queue_ids:
            queue_row = (await db.execute(select(PrinterQueue).where(PrinterQueue.id == q_id))).scalar_one_or_none()
            if queue_row is None:
                continue
            virtual = await build_virtual_current_print(db, queue_row.printer_id)
            if virtual:
                enriched.insert(0, virtual)

    return enriched


@router.post("/", response_model=PrintQueueItemResponse)
async def add_to_queue(
    data: PrintQueueItemCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
):
    """Add an item to the print queue."""
    # Validate that either archive_id or library_file_id is provided
    if not data.archive_id and not data.library_file_id:
        raise HTTPException(400, "Either archive_id or library_file_id must be provided")

    # Validate queue exists
    result = await db.execute(
        select(PrinterQueue).options(selectinload(PrinterQueue.printer)).where(PrinterQueue.id == data.queue_id)
    )
    queue = result.scalar_one_or_none()
    if not queue:
        raise HTTPException(400, "Queue not found")

    # Validate archive exists (if provided) and get it for filament extraction
    archive = None
    if data.archive_id:
        result = await db.execute(select(PrintArchive).where(PrintArchive.id == data.archive_id))
        archive = result.scalar_one_or_none()
        if not archive:
            raise HTTPException(400, "Archive not found")

    # Validate library file exists (if provided) and get it for filament extraction.
    # m044: eager-load M2M projects so the fallback below doesn't lazy-fetch.
    library_file = None
    if data.library_file_id:
        result = await db.execute(
            select(LibraryFile)
            .options(selectinload(LibraryFile.projects))
            .where(LibraryFile.id == data.library_file_id)
        )
        library_file = result.scalar_one_or_none()
        if not library_file:
            raise HTTPException(400, "Library file not found")

    # Get next position for this queue
    result = await db.execute(
        select(func.max(PrintQueueItem.position))
        .where(PrintQueueItem.queue_id == data.queue_id)
        .where(PrintQueueItem.status == "pending")
    )
    max_pos = result.scalar() or 0

    # Validate project exists before insert so a bogus ID yields 404, not an FK-constraint 500
    if data.project_id is not None:
        project_result = await db.execute(select(Project).where(Project.id == data.project_id))
        if not project_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Project not found")

    # Fallback: if the caller didn't pass a project_id but the source is a
    # library file that's already linked to one or more projects, inherit
    # the first one. Queue items stay single-project by design — for a
    # multi-project file the operator should pass ``project_id`` explicitly
    # to disambiguate. m044: was previously a single FK, now a list, so we
    # pick ``[0]`` for the fallback (deterministic — pivot rows are read in
    # insertion order via the relationship).
    effective_project_id = data.project_id
    if effective_project_id is None and library_file and library_file.projects:
        effective_project_id = library_file.projects[0].id

    # For quantity > 1, group copies under a shared batch_id
    batch_id = str(uuid.uuid4()) if data.quantity > 1 else None
    ams_mapping_json = json.dumps(data.ams_mapping) if data.ams_mapping else None

    # Swap-macro execution is only meaningful when (a) the target printer has
    # swap mode on AND (b) the source file does not already carry swap macros
    # baked in by third-party tooling (``swap_compatible``). Otherwise force
    # the feature off so stored state never lies about what fires at dispatch
    # and we don't double-execute macros.
    printer_swap_on = bool(queue.printer and queue.printer.swap_mode_enabled)
    source_has_baked_macros = bool(
        (archive and getattr(archive, "swap_compatible", False))
        or (library_file and getattr(library_file, "swap_compatible", False))
    )
    execute_swap_macros = bool(data.execute_swap_macros) and printer_swap_on and not source_has_baked_macros
    swap_macro_events_json = (
        json.dumps(data.swap_macro_events) if execute_swap_macros and data.swap_macro_events else None
    )

    items: list[PrintQueueItem] = []
    for i in range(data.quantity):
        items.append(
            PrintQueueItem(
                queue_id=data.queue_id,
                archive_id=data.archive_id,
                library_file_id=data.library_file_id,
                scheduled_time=data.scheduled_time,
                auto_off_after=data.auto_off_after,
                manual_start=data.manual_start,
                ams_mapping=ams_mapping_json,
                plate_id=data.plate_id,
                bed_levelling=data.bed_levelling,
                flow_cali=data.flow_cali,
                layer_inspect=data.layer_inspect,
                timelapse=data.timelapse,
                use_ams=data.use_ams,
                mesh_mode_fast_check=data.mesh_mode_fast_check,
                execute_swap_macros=execute_swap_macros,
                swap_macro_events=swap_macro_events_json,
                gcode_injection=data.gcode_injection,
                project_id=effective_project_id,
                position=max_pos + 1 + i,
                status="pending",
                batch_id=batch_id,
                created_by_id=current_user.id if current_user else None,
            )
        )
    db.add_all(items)
    await db.commit()
    for it in items:
        await db.refresh(it)
    item = items[0]

    # Update queue counters (full recount for accuracy)
    from backend.app.services.queue_counters import update_queue_counters

    await update_queue_counters(db, data.queue_id)
    await db.commit()

    # Re-query with full eager loading (queue→printer chain)
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .where(PrintQueueItem.id == item.id)
    )
    item = result.scalar_one()

    source_name = f"archive {data.archive_id}" if data.archive_id else f"library file {data.library_file_id}"
    target_desc = queue.printer.name if queue.printer else f"queue {data.queue_id}"
    logger.info("Added %s to queue for %s", source_name, target_desc)

    # MQTT relay - publish queue job added
    try:
        from backend.app.services.mqtt_relay import mqtt_relay

        await mqtt_relay.on_queue_job_added(
            job_id=item.id,
            filename=item.archive.filename if item.archive else "",
            printer_id=item.printer_id,
            printer_name=queue.printer.name if queue.printer else None,
        )
    except Exception:
        pass  # Don't fail queue add if MQTT fails

    # Send notification for job added
    try:
        job_name = (
            item.archive.filename
            if item.archive
            else item.library_file.filename
            if item.library_file
            else f"Job #{item.id}"
        )
        job_name = job_name.replace(".gcode.3mf", "").replace(".3mf", "")
        target = queue.printer.name if queue.printer else f"Queue #{data.queue_id}"
        await notification_service.on_queue_job_added(
            job_name=job_name,
            target=target,
            db=db,
            printer_id=item.printer_id,
            printer_name=target,
        )
    except Exception:
        pass  # Don't fail queue add if notification fails

    return _enrich_response(item)


@router.patch("/bulk", response_model=PrintQueueBulkUpdateResponse)
async def bulk_update_queue_items(
    data: PrintQueueBulkUpdate,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_UPDATE_ALL,
            Permission.QUEUE_UPDATE_OWN,
        )
    ),
):
    """Bulk update multiple queue items with the same values.

    Only pending items can be updated. Non-pending items are skipped.
    Items not owned by the user are also skipped (unless user has *_all permission).
    """
    user, can_modify_all = auth_result

    if not data.item_ids:
        raise HTTPException(400, "No item IDs provided")

    # Get fields to update (exclude item_ids and unset fields)
    update_data = data.model_dump(exclude={"item_ids"}, exclude_unset=True)
    if not update_data:
        raise HTTPException(400, "No fields to update")

    # Validate queue_id if being changed
    if "queue_id" in update_data and update_data["queue_id"] is not None:
        result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == update_data["queue_id"]))
        if not result.scalar_one_or_none():
            raise HTTPException(400, "Queue not found")

    # Fetch all items
    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(data.item_ids)))
    items = result.scalars().all()

    updated_count = 0
    skipped_count = 0

    for item in items:
        if item.status != "pending":
            skipped_count += 1
            continue

        # Ownership check
        if not can_modify_all and item.created_by_id != user.id:
            skipped_count += 1
            continue

        for field, value in update_data.items():
            setattr(item, field, value)
        updated_count += 1

    await db.commit()

    logger.info("Bulk updated %s queue items, skipped %s", updated_count, skipped_count)
    return PrintQueueBulkUpdateResponse(
        updated_count=updated_count,
        skipped_count=skipped_count,
        message=f"Updated {updated_count} items"
        + (f", skipped {skipped_count} non-pending/not-owned" if skipped_count else ""),
    )


@router.get("/{item_id}", response_model=PrintQueueItemResponse)
async def get_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ),
):
    """Get a specific queue item."""
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .where(PrintQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    return _enrich_response(item)


@router.patch("/{item_id}", response_model=PrintQueueItemResponse)
async def update_queue_item(
    item_id: int,
    data: PrintQueueItemUpdate,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_UPDATE_ALL,
            Permission.QUEUE_UPDATE_OWN,
        )
    ),
):
    """Update a queue item."""
    user, can_modify_all = auth_result

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    # Ownership check
    if not can_modify_all:
        if item.created_by_id != user.id:
            raise HTTPException(403, "You can only update your own queue items")

    if item.status != "pending":
        raise HTTPException(400, "Can only update pending items")

    update_data = data.model_dump(exclude_unset=True)

    # Validate new queue_id if being changed
    if "queue_id" in update_data and update_data["queue_id"] is not None:
        result = await db.execute(select(PrinterQueue).where(PrinterQueue.id == update_data["queue_id"]))
        if not result.scalar_one_or_none():
            raise HTTPException(400, "Queue not found")

    # Serialize ams_mapping to JSON for TEXT column storage
    if "ams_mapping" in update_data:
        update_data["ams_mapping"] = json.dumps(update_data["ams_mapping"]) if update_data["ams_mapping"] else None

    # swap_macro_events is stored as a JSON-encoded TEXT column.
    if "swap_macro_events" in update_data:
        events = update_data["swap_macro_events"]
        update_data["swap_macro_events"] = json.dumps(events) if events else None

    for field, value in update_data.items():
        setattr(item, field, value)

    await db.commit()

    # Re-query with full eager loading (queue→printer chain)
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .where(PrintQueueItem.id == item_id)
    )
    item = result.scalar_one()

    logger.info("Updated queue item %s", item_id)
    return _enrich_response(item)


@router.delete("/{item_id}")
async def delete_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_DELETE_ALL,
            Permission.QUEUE_DELETE_OWN,
        )
    ),
):
    """Remove an item from the queue."""
    user, can_modify_all = auth_result

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    # Ownership check
    if not can_modify_all:
        if item.created_by_id != user.id:
            raise HTTPException(403, "You can only delete your own queue items")

    if item.status == "printing":
        raise HTTPException(400, "Cannot delete item that is currently printing")

    queue_id = item.queue_id
    await db.delete(item)

    from backend.app.services.queue_counters import update_queue_counters

    await update_queue_counters(db, queue_id)
    await db.commit()

    logger.info("Deleted queue item %s", item_id)
    return {"message": "Queue item deleted"}


@router.post("/reorder")
async def reorder_queue(
    data: PrintQueueReorder,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Bulk update positions for queue items."""
    for reorder_item in data.items:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == reorder_item.id))
        item = result.scalar_one_or_none()
        if item and item.status == "pending":
            item.position = reorder_item.position

    await db.commit()
    logger.info("Reordered %s queue items", len(data.items))
    return {"message": f"Reordered {len(data.items)} items"}


@router.post("/{item_id}/cancel")
async def cancel_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_UPDATE_ALL,
            Permission.QUEUE_UPDATE_OWN,
        )
    ),
):
    """Cancel a pending queue item."""
    user, can_modify_all = auth_result

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    # Ownership check
    if not can_modify_all:
        if item.created_by_id != user.id:
            raise HTTPException(403, "You can only cancel your own queue items")

    if item.status not in ("pending",):
        raise HTTPException(400, f"Cannot cancel item with status '{item.status}'")

    item.status = "cancelled"
    item.completed_at = datetime.now(timezone.utc)

    from backend.app.services.queue_counters import update_queue_counters

    await update_queue_counters(db, item.queue_id)
    await db.commit()

    logger.info("Cancelled queue item %s", item_id)
    return {"message": "Queue item cancelled"}


@router.post("/{item_id}/stop")
async def stop_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Stop an actively printing queue item."""
    import asyncio

    from backend.app.models.smart_plug import SmartPlug
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.tasmota import tasmota_service

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    if item.status != "printing":
        raise HTTPException(400, f"Can only stop items that are printing, current status: '{item.status}'")

    # Capture values we need for background task (queue_id == printer_id)
    printer_id = item.queue_id
    auto_off_after = item.auto_off_after

    # re-Connect MQTT if stalled
    if not await printer_manager.ensure_fresh_connection(printer_id):
        logger.warning(
            "ensure_fresh_connection returned False for printer %s - printer may not be connected", printer_id
        )

    # Try to send stop command to printer
    stop_sent = False
    try:
        stop_sent = printer_manager.stop_print(printer_id)
        if not stop_sent:
            logger.warning("stop_print returned False for printer %s - printer may not be connected", printer_id)
    except Exception as e:
        logger.error("Error sending stop command for queue item %s: %s", item_id, e)

    # Mark this printer as user-stopped BEFORE the first await so that if the
    # MQTT on_print_complete callback fires during the db.commit() yield the flag
    # is already set and the "failed" status will be correctly overridden to
    # "cancelled" (preventing a spurious "print failed" notification).
    try:
        from backend.app.main import mark_printer_stopped_by_user

        mark_printer_stopped_by_user(printer_id)
    except Exception as _mark_err:
        logger.warning("Failed to mark printer %s as user-stopped: %s", printer_id, _mark_err)

    # Update queue item status regardless - if printer is off, print is already stopped
    item.status = "cancelled"
    item.completed_at = datetime.now(timezone.utc)
    item.error_message = "Stopped by user" if stop_sent else "Stopped by user (printer was offline)"

    from backend.app.services.queue_counters import set_queue_idle, update_queue_counters

    await set_queue_idle(db, item.queue_id)
    await update_queue_counters(db, item.queue_id)
    await db.commit()

    # Get smart plug info if auto-off is enabled
    plug_ip = None
    if auto_off_after:
        result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
        plug = result.scalar_one_or_none()
        if plug and plug.enabled:
            plug_ip = plug.ip_address

    logger.info("Stopped printing queue item %s (stop command sent: %s)", item_id, stop_sent)

    # Schedule background task for cooldown + power off
    if plug_ip:

        async def cooldown_and_poweroff():
            logger.info("Auto-off: Waiting for printer %s to cool down before power off...", printer_id)
            await printer_manager.wait_for_cooldown(printer_id, target_temp=50.0, timeout=600)
            # Re-fetch plug since we're in a new async context
            from backend.app.core.database import async_session

            async with async_session() as new_db:
                result = await new_db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
                plug = result.scalar_one_or_none()
                if plug and plug.enabled:
                    logger.info("Auto-off: Powering off printer %s", printer_id)
                    await tasmota_service.turn_off(plug)

        asyncio.create_task(cooldown_and_poweroff())

    return {"message": "Print stopped" if stop_sent else "Queue item cancelled (printer was offline)"}


@router.post("/{item_id}/start")
async def start_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_OWN),
):
    """Manually start a staged (manual_start) queue item.

    This clears the manual_start flag so the scheduler will pick it up,
    or starts immediately if the printer is ready.
    """
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
        )
        .where(PrintQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    if item.status != "pending":
        raise HTTPException(400, f"Can only start pending items, current status: '{item.status}'")

    # Clear manual_start flag so scheduler picks it up
    item.manual_start = False
    await db.commit()

    # Re-query with full eager loading (queue→printer chain)
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .where(PrintQueueItem.id == item_id)
    )
    item = result.scalar_one()

    logger.info("Manually started queue item %s (cleared manual_start flag)", item_id)
    return _enrich_response(item)


# ============================================================================
# Reorder / bump / clone / skip / retry — single-item operations
# ============================================================================


@router.post("/{item_id}/reorder")
async def reorder_item(
    item_id: int,
    direction: str = Query(..., pattern="^(up|down)$"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a single queue item (or its whole batch) one step up/down.

    Batch cohesion: if the item has a ``batch_id``, the entire block
    of pending batch siblings moves together.
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import reorder_block, resolve_block_ids

    queue_id, block_ids = await resolve_block_ids(db, item_id)
    if not block_ids:
        raise HTTPException(404, "Queue item not found")

    moved = await reorder_block(db, queue_id, block_ids, direction)
    if moved:
        await update_queue_counters(db, queue_id)
        await db.commit()
    return {"moved": moved, "direction": direction, "block_size": len(block_ids)}


@router.post("/{item_id}/bump")
async def bump_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move an item (and its batch) to the top of its queue."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_top, resolve_block_ids

    queue_id, block_ids = await resolve_block_ids(db, item_id)
    if not block_ids:
        raise HTTPException(404, "Queue item not found")

    shifted = await bump_block_to_top(db, queue_id, block_ids)
    if shifted:
        await update_queue_counters(db, queue_id)
        await db.commit()
    return {"shifted": shifted, "block_size": len(block_ids)}


@router.post("/{item_id}/bump-bottom")
async def bump_item_bottom(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move an item (and its batch) to the bottom of its queue."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_bottom, resolve_block_ids

    queue_id, block_ids = await resolve_block_ids(db, item_id)
    if not block_ids:
        raise HTTPException(404, "Queue item not found")

    shifted = await bump_block_to_bottom(db, queue_id, block_ids)
    if shifted:
        await update_queue_counters(db, queue_id)
        await db.commit()
    return {"shifted": shifted, "block_size": len(block_ids)}


@router.post("/{item_id}/clone", response_model=PrintQueueItemResponse)
async def clone_item_endpoint(
    item_id: int,
    scope: str = Query("single", pattern="^(single|batch)$"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_CREATE),
):
    """Clone a queue item.

    ``scope='single'`` — insert one duplicate, share ``batch_id`` if
    source has one (so the new copy becomes a sibling in the same
    batch).  ``scope='batch'`` — clone the entire batch into a new
    batch.  Returns the first cloned item.
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import clone_batch, clone_item

    src = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not src:
        raise HTTPException(404, "Queue item not found")

    if scope == "batch":
        if not src.batch_id:
            raise HTTPException(400, "Item is not part of a batch")
        clones = await clone_batch(db, src.batch_id)
        if not clones:
            raise HTTPException(400, "No pending items in batch to clone")
        await update_queue_counters(db, clones[0].queue_id)
        await db.commit()
        first = clones[0]
    else:
        first = await clone_item(db, item_id, keep_batch=True)
        if first is None:
            raise HTTPException(500, "Clone failed")
        await update_queue_counters(db, first.queue_id)
        await db.commit()

    # Re-fetch with full eager loading for response.
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .where(PrintQueueItem.id == first.id)
    )
    return _enrich_response(result.scalar_one())


@router.post("/{item_id}/skip")
async def skip_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Set a pending item's status to ``skipped`` — scheduler won't pick it."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import set_status

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Only pending items can be skipped, current status: '{item.status}'")

    await set_status(db, item_id, "skipped")
    await update_queue_counters(db, item.queue_id)
    await db.commit()
    return {"status": "skipped", "item_id": item_id}


@router.post("/{item_id}/unskip")
async def unskip_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Revert a skipped item back to pending, appended to end of queue."""
    from backend.app.services.queue_counters import update_queue_counters

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "skipped":
        raise HTTPException(400, f"Only skipped items can be unskipped, current status: '{item.status}'")

    max_pos = (
        await db.execute(
            select(func.max(PrintQueueItem.position))
            .where(PrintQueueItem.queue_id == item.queue_id)
            .where(PrintQueueItem.status == "pending")
        )
    ).scalar() or 0
    item.status = "pending"
    item.position = max_pos + 1
    await update_queue_counters(db, item.queue_id)
    await db.commit()
    return {"status": "pending", "item_id": item_id}


@router.patch("/{item_id}/manual-start")
async def toggle_manual_start(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Toggle the ``manual_start`` flag on a pending item.

    If the item is part of a batch, toggle is propagated to all pending
    siblings so the batch behaves consistently.
    """
    from backend.app.services.queue_ops import get_batch_pending_items

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Only pending items can be toggled, current status: '{item.status}'")

    new_value = not item.manual_start
    if item.batch_id:
        for sibling in await get_batch_pending_items(db, item.batch_id):
            sibling.manual_start = new_value
    else:
        item.manual_start = new_value
    await db.commit()
    return {"manual_start": new_value, "item_id": item_id}


@router.post("/{item_id}/retry", response_model=PrintQueueItemResponse)
async def retry_failed_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Put a failed item back into pending status, appended to end of queue."""
    from backend.app.services.queue_counters import update_queue_counters

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "failed":
        raise HTTPException(400, f"Only failed items can be retried, current status: '{item.status}'")

    max_pos = (
        await db.execute(
            select(func.max(PrintQueueItem.position))
            .where(PrintQueueItem.queue_id == item.queue_id)
            .where(PrintQueueItem.status == "pending")
        )
    ).scalar() or 0
    item.status = "pending"
    item.position = max_pos + 1
    item.error_message = None
    item.completed_at = None
    await update_queue_counters(db, item.queue_id)
    await db.commit()

    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
        )
        .where(PrintQueueItem.id == item_id)
    )
    return _enrich_response(result.scalar_one())


# ============================================================================
# Batch-level operations
# ============================================================================


@router.post("/batch/{batch_id}/cancel")
async def cancel_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Cancel all pending items in a batch.  Active (printing) item is unaffected."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import get_batch_pending_items, set_status_for_batch

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        return {"cancelled": 0}
    queue_id = pending[0].queue_id
    count = await set_status_for_batch(db, batch_id, "cancelled")
    await update_queue_counters(db, queue_id)
    await db.commit()
    return {"cancelled": count, "batch_id": batch_id}


@router.post("/batch/{batch_id}/skip")
async def skip_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Skip all pending items in a batch."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import get_batch_pending_items, set_status_for_batch

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        return {"skipped": 0}
    queue_id = pending[0].queue_id
    count = await set_status_for_batch(db, batch_id, "skipped")
    await update_queue_counters(db, queue_id)
    await db.commit()
    return {"skipped": count, "batch_id": batch_id}


@router.post("/batch/{batch_id}/reorder")
async def reorder_batch(
    batch_id: str,
    direction: str = Query(..., pattern="^(up|down)$"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a whole batch block one step up/down."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import get_batch_pending_items, reorder_block

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    queue_id = pending[0].queue_id
    block_ids = [i.id for i in pending]
    moved = await reorder_block(db, queue_id, block_ids, direction)
    if moved:
        await update_queue_counters(db, queue_id)
        await db.commit()
    return {"moved": moved, "direction": direction, "batch_size": len(block_ids)}


@router.post("/batch/{batch_id}/bump")
async def bump_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a whole batch to the top of its queue."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_top, get_batch_pending_items

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    queue_id = pending[0].queue_id
    block_ids = [i.id for i in pending]
    shifted = await bump_block_to_top(db, queue_id, block_ids)
    if shifted:
        await update_queue_counters(db, queue_id)
        await db.commit()
    return {"shifted": shifted, "batch_size": len(block_ids)}


@router.post("/batch/{batch_id}/bump-bottom")
async def bump_batch_bottom(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a whole batch to the bottom of its queue."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_bottom, get_batch_pending_items

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    queue_id = pending[0].queue_id
    block_ids = [i.id for i in pending]
    shifted = await bump_block_to_bottom(db, queue_id, block_ids)
    if shifted:
        await update_queue_counters(db, queue_id)
        await db.commit()
    return {"shifted": shifted, "batch_size": len(block_ids)}


@router.patch("/batch/{batch_id}")
async def update_batch(
    batch_id: str,
    data: PrintQueueItemUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Apply a partial update to every pending item in the batch."""
    from backend.app.services.queue_ops import get_batch_pending_items

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        raise HTTPException(404, "No pending items in batch")

    update_data = data.model_dump(exclude_unset=True)
    if "ams_mapping" in update_data:
        update_data["ams_mapping"] = json.dumps(update_data["ams_mapping"]) if update_data["ams_mapping"] else None
    if "swap_macro_events" in update_data:
        events = update_data["swap_macro_events"]
        update_data["swap_macro_events"] = json.dumps(events) if events else None

    for item in pending:
        for field, value in update_data.items():
            setattr(item, field, value)
    await db.commit()
    return {"updated": len(pending), "batch_id": batch_id, "fields": list(update_data.keys())}


@router.post("/batch/{batch_id}/clone")
async def clone_batch_endpoint(
    batch_id: str,
    scope: str = Query("batch", pattern="^(one|batch)$"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_CREATE),
):
    """Clone a batch.

    ``scope='one'`` — add one more copy to the same batch (appended).
    ``scope='batch'`` — create a whole new batch with the same
    configuration as the source.
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import clone_batch, clone_item, get_batch_pending_items

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    queue_id = pending[0].queue_id

    if scope == "one":
        new_item = await clone_item(db, pending[0].id, keep_batch=True)
        if new_item is None:
            raise HTTPException(500, "Clone failed")
        await update_queue_counters(db, queue_id)
        await db.commit()
        return {"cloned": 1, "scope": "one", "batch_id": batch_id, "new_item_id": new_item.id}

    clones = await clone_batch(db, batch_id)
    if not clones:
        raise HTTPException(500, "Clone failed")
    await update_queue_counters(db, queue_id)
    await db.commit()
    return {
        "cloned": len(clones),
        "scope": "batch",
        "source_batch_id": batch_id,
        "new_batch_id": clones[0].batch_id,
    }
