"""API routes for print queue management."""

import asyncio
import json
import logging
import uuid
import zipfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, inspect as sa_inspect, select
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
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import derive_mode, normalize_mode
from backend.app.schemas.farm_forecast import FarmForecastOut
from backend.app.schemas.print_queue import (
    PrintQueueBatchCreate,
    PrintQueueBulkUpdate,
    PrintQueueBulkUpdateResponse,
    PrintQueueItemCreate,
    PrintQueueItemResponse,
    PrintQueueItemUpdate,
    PrintQueueReorder,
)
from backend.app.services import farm_forecast, queue_sources
from backend.app.services.filament_intake import item_descriptor, loaded_descriptor, source_display_filename
from backend.app.services.filament_policy import decode
from backend.app.services.filament_policy_write import routing_update
from backend.app.services.notification_service import notification_service
from backend.app.services.queue_add import add_items_to_printer_queue
from backend.app.services.queue_source_capture import refusal, reusing_sources
from backend.app.services.queue_source_descriptor import source_storage_state
from backend.app.services.queue_sources import QueueSourceError
from backend.app.services.queue_times import plate_metadata_cached, plate_metadata_for_row, plate_picture_for_row
from backend.app.services.source_io import SourceUnavailable
from backend.app.utils.printer_models import is_gcode_compatible
from backend.app.utils.threemf_tools import plate_picture_entry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"])


# The three tri-state calibration fields. Each has a legacy bool column
# (authoritative for off/on — read by the dispatcher + secondary sites) and a
# nullable ``*_mode`` column that carries the extra 'auto' state.
_CALI_MODE_FIELDS = ("bed_levelling", "flow_cali", "nozzle_offset_cali")


def _set_calibration_mode(item: PrintQueueItem, field: str, value) -> None:
    """Store a tri-state calibration value on ``item``: the legacy bool mirror
    (``field``) plus the ``field_mode`` column. ``value`` is a CalibrationMode
    string (the schema coercer already normalised any legacy bool), but a raw
    bool is tolerated defensively."""
    mode = normalize_mode(value)
    setattr(item, field, mode == "on")
    setattr(item, f"{field}_mode", mode)


def _enrich_response(item: PrintQueueItem) -> PrintQueueItemResponse:
    """Add nested archive/printer/library_file info to response.

    ⚠️ **The job's own captured bytes describe it, not its original rows** (m173,
    spec §4, A09). Those rows may be gone — trashed, purged, retention-swept —
    while the job still prints perfectly, and before this read such a row came
    back untimed, unplated and named ``File #undefined``. ``descriptor`` is the
    snapshot when the query eager-loaded ``queue_source``; the archive / library
    branches below still fill everything they always did, so a legacy row is
    answered byte-identically and a snapshotted row whose rows survive simply has
    its three per-plate numbers and its name come out of the frozen copy instead.
    """
    descriptor = loaded_descriptor(item)
    source = item.queue_source if descriptor is not None else None
    snapshot_time, snapshot_grams, snapshot_bed = plate_metadata_for_row(plate_id=item.plate_id, descriptor=descriptor)
    # Parse ams_mapping from JSON string BEFORE model_validate
    ams_mapping_parsed = None
    if item.ams_mapping:
        try:
            ams_mapping_parsed = json.loads(item.ams_mapping)
        except json.JSONDecodeError:
            ams_mapping_parsed = None

    # Parse nozzle_mapping from JSON string (#1780 — H2C rack slicer-pick
    # preservation). Nullable opaque JSON blob stored verbatim from
    # BambuStudio's project_file; surface it parsed for the response model.
    nozzle_mapping_parsed = None
    if item.nozzle_mapping:
        try:
            nozzle_mapping_parsed = json.loads(item.nozzle_mapping)
        except json.JSONDecodeError:
            nozzle_mapping_parsed = None

    # Create response with parsed ams_mapping
    item_dict = {
        "id": item.id,
        "queue_id": item.queue_id,
        "printer_id": item.printer_id,  # convenience property from queue
        "project_id": item.project_id,
        "project_line_id": item.project_line_id,
        "waiting_reason": item.waiting_reason,
        "archive_id": item.archive_id,
        "library_file_id": item.library_file_id,
        "position": item.position,
        "scheduled_time": item.scheduled_time,
        "auto_off_after": item.auto_off_after,
        "manual_start": item.manual_start,
        "require_previous_success": item.require_previous_success,
        "ams_mapping": ams_mapping_parsed,
        "filament_routing": (value if isinstance(value := decode(item.filament_routing), dict) else None),
        "origin": item.origin,
        "plate_id": item.plate_id,
        "bed_levelling": derive_mode(item.bed_levelling_mode, item.bed_levelling),
        "flow_cali": derive_mode(item.flow_cali_mode, item.flow_cali),
        "layer_inspect": item.layer_inspect,
        "timelapse": item.timelapse,
        "timelapse_storage": item.timelapse_storage,
        "use_ams": item.use_ams,
        "nozzle_offset_cali": derive_mode(item.nozzle_offset_cali_mode, item.nozzle_offset_cali),
        "mesh_mode_fast_check": item.mesh_mode_fast_check,
        "execute_swap_macros": item.execute_swap_macros,
        "swap_macro_events": json.loads(item.swap_macro_events) if item.swap_macro_events else None,
        "selected_macro_ids": json.loads(item.selected_macro_ids) if item.selected_macro_ids else None,
        "gcode_injection": item.gcode_injection,
        "preheat_override": getattr(item, "preheat_override", "inherit"),
        "preheat_chamber_target_override": getattr(item, "preheat_chamber_target_override", None),
        # H2C rack-swap nozzle pick (#1780)
        "nozzle_mapping": nozzle_mapping_parsed,
        "status": item.status,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "batch_id": item.batch_id,
        # User tracking (Issue #206)
        "created_by_id": item.created_by_id,
        "created_by_username": item.created_by.username if item.created_by else None,
        # m173. ``blob_state`` comes from the row the query eager-loads for
        # ``descriptor`` below; a caller that did not load it answers ``legacy``,
        # because ``ready`` is a promise that the bytes are there and this builder
        # may not make one it has not checked.
        "source_storage": source_storage_state(
            queue_source_id=item.queue_source_id,
            blob_state=source.state if source is not None else None,
            origin=item.origin,
            is_calibration=item.is_calibration,
        ),
        "source_size_bytes": source.size_bytes if source is not None else None,
        # Whether this row's own bytes render its plate (spec §4 / A09). A job
        # whose original rows are gone has no id from which the frontend could
        # build a thumbnail URL, so it has to be TOLD that it has a picture —
        # inferring one from the format would put a broken image on every 3MF that
        # carries no render, and on every raw G-code source. Costs a namelist read
        # per (object, plate) per process, cached beside the per-plate metadata;
        # the picture itself is served by ``get_source_thumbnail`` below, which
        # this list deliberately never calls.
        "source_thumbnail": plate_picture_for_row(plate_id=item.plate_id, descriptor=descriptor) is not None,
    }
    response = PrintQueueItemResponse(**item_dict)
    # ⚠️ Only when the relationship is already loaded. This runs in async
    # request handlers, where touching an unloaded relationship is not a lazy
    # query but a MissingGreenlet — and not every caller eager-loads the order.
    # An endpoint that did not load it answers ``project_name=None``; the ones
    # the copy-queue dialog reads from do load it.
    if item.project_id is not None and "project" not in sa_inspect(item).unloaded:
        response.project_name = item.project.name if item.project else None
    if item.archive:
        # Soft-deleted (trashed) archive: the row survives but its files are
        # gone from disk. Suppress the archive-derived surface so we never
        # serve a thumbnail_path / plate data that 404s, and flag the row so
        # the UI can show a "source deleted" state. Pending items linked to it
        # were already cancelled at trash time (#1348 follow-up).
        if item.archive.deleted_at is not None:
            response.archive_deleted = True
        else:
            response.archive_name = item.archive.print_name or item.archive.filename
            response.archive_thumbnail = item.archive.thumbnail_path
            response.print_time_seconds = item.archive.print_time_seconds
            response.filament_used_grams = item.archive.filament_used_grams
            response.filament_type = item.archive.filament_type
            response.filament_color = item.archive.filament_color
            response.layer_height = item.archive.layer_height
            response.nozzle_diameter = item.archive.nozzle_diameter
            response.sliced_for_model = item.archive.sliced_for_model
            response.bed_type = item.archive.bed_type
            # ⚠️ ``descriptor is None``: a job that owns its bytes is never
            # described from the ORIGINAL's file. The columns above are fair — they
            # were read out of the bytes this job accepted — but the file on disk
            # may have been re-sliced since (A03), and a card showing a number out
            # of bytes the job will not print is the thing this feature exists to
            # stop. The snapshot's own three values are applied below.
            if item.plate_id and descriptor is None:
                archive_path = settings.base_dir / item.archive.file_path
                # ⚠️ ``is_file()``, never ``exists()``. An archive created at
                # print start carries ``file_path=""`` until its 3MF is fetched,
                # and an empty path resolves to ``base_dir`` — a directory,
                # which ``exists()`` happily confirms. The three parsers below
                # then each opened the data directory as a ZIP and logged
                # "Failed to extract print time from /app/data: Is a directory"
                # every time a browser polled the queue during that window.
                if archive_path.is_file():
                    plate_time, plate_weight, plate_bed = plate_metadata_cached(archive_path, item.plate_id)
                    if plate_time is not None:
                        response.print_time_seconds = plate_time
                    if plate_weight > 0:
                        response.filament_used_grams = plate_weight
                    if plate_bed:
                        response.bed_type = plate_bed
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
            response.bed_type = item.library_file.file_metadata.get("bed_type")
        # Same rule as the archive branch above: not for a job with a snapshot.
        if item.plate_id and descriptor is None:
            lib_path = Path(item.library_file.file_path)
            library_file_path = lib_path if lib_path.is_absolute() else settings.base_dir / item.library_file.file_path
            # Same guard as the archive branch above — a blank ``file_path``
            # resolves to a directory, not to nothing.
            if library_file_path.is_file():
                plate_time, plate_weight, plate_bed = plate_metadata_cached(library_file_path, item.plate_id)
                if plate_time is not None:
                    response.print_time_seconds = plate_time
                if plate_weight > 0:
                    response.filament_used_grams = plate_weight
                if plate_bed:
                    response.bed_type = plate_bed
    if descriptor is not None:
        # LAST, so the frozen copy outranks both rows. Each value is applied only
        # when the snapshot actually has it: a plate with no ``prediction`` leaves
        # the row's own recorded COLUMN standing, which came out of these same
        # bytes — the original's *file* was already excluded above.
        if snapshot_time is not None:
            response.print_time_seconds = snapshot_time
        if snapshot_grams > 0:
            response.filament_used_grams = snapshot_grams
        if snapshot_bed:
            response.bed_type = snapshot_bed
        if not response.archive_name and not response.library_file_name:
            # ``QueueCard`` renders ``archive_name || library_file_name ||
            # 'File #' + (archive_id || library_file_id)`` — with both ids NULL the
            # third answer is "File #undefined", so the job's own display name is
            # the only thing that can name it. It goes in the field its provenance
            # would have filled, and never as the object's hash, which
            # ``source_display_filename`` refuses (§4, A04).
            with suppress(SourceUnavailable):
                name = source_display_filename(descriptor)
                if descriptor.provenance.get("kind") == "archive":
                    response.archive_name = name
                else:
                    response.library_file_name = name
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


@router.get("/forecast", response_model=FarmForecastOut)
async def get_queue_forecast(
    db: AsyncSession = Depends(get_db), _: User | None = RequirePermission(Permission.QUEUE_READ)
):
    """When the last printer is free, given what the queues already hold — the
    number the queue page's stats bar shows (spec 2026-09-06, Decision 5)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    farm = farm_forecast.simulate_farm(await farm_forecast.load_snapshot(db, now))
    return FarmForecastOut.of(now, farm)


@router.get("/", response_model=list[PrintQueueItemResponse])
async def list_queue(
    queue_id: int | None = Query(None, description="Filter by printer queue"),
    status: str | None = Query(None, description="Filter by status"),
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_READ_ALL,
            Permission.QUEUE_READ_OWN,
        )
    ),
):
    """List all queue items, optionally filtered by queue or status."""
    user, can_read_all = auth_result
    query = (
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
        )
        .order_by(PrintQueueItem.queue_id, PrintQueueItem.position)
    )
    if user is not None and not can_read_all:
        query = query.where(PrintQueueItem.created_by_id == user.id)

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
    # Every gate, the advisory lock, the position and the build live in
    # ``services/queue_add`` so the file manager's bulk add cannot become a
    # second definition of what a queue item is.
    items, queue = await add_items_to_printer_queue(db, data, current_user)
    # Captured before the re-query below, which fetches one row by id: the
    # service's own list is the only place every created row is named.
    created_item_ids = [i.id for i in items]
    item = items[0]

    # Re-query with full eager loading (queue→printer chain)
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
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

    response = _enrich_response(item)
    response.created_item_ids = created_item_ids
    return response


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

        item_update = await routing_update(db, item, update_data)
        if isinstance(item_update.get("ams_mapping"), list):
            item_update["ams_mapping"] = json.dumps(item_update["ams_mapping"])
        for field, value in item_update.items():
            if field in _CALI_MODE_FIELDS:
                _set_calibration_mode(item, field, value)
            else:
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
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_READ_ALL,
            Permission.QUEUE_READ_OWN,
        )
    ),
):
    """Get a specific queue item."""
    current_user, can_read_all = auth_result
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
        )
        .where(PrintQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if (
        current_user is not None
        and not can_read_all
        and (item.created_by_id is None or item.created_by_id != current_user.id)
    ):
        raise HTTPException(404, "Queue item not found")
    return _enrich_response(item)


def _read_plate_picture(path: Path, plate_id: int | None) -> bytes | None:
    """The PNG bytes of one plate's render, read off the loop (S10).

    ``None`` — never an exception — for a container that is gone, is not a ZIP, or
    simply does not render this plate: a missing picture is not an error (spec §4).
    The entry is named by :func:`~backend.app.utils.threemf_tools.plate_picture_entry`,
    the same resolver the list's flag asks, so the two cannot disagree about which
    plate was meant.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            entry = plate_picture_entry(zf, plate_id)
            return None if entry is None else zf.read(entry)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


@router.get("/{item_id}/source-thumbnail")
async def get_source_thumbnail(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_READ_ALL,
            Permission.QUEUE_READ_OWN,
        )
    ),
):
    """The picture of a queued job's own captured bytes (m173, spec §4 / A09).

    A job that owns a snapshot prints after its library file or archive is gone —
    and must be able to SHOW itself after that too, which no ``archive_id`` /
    ``library_file_id`` URL can do. So the plate's render is served straight out
    of the job's object here, and the list's ``source_thumbnail`` flag says in
    advance whether there is one.

    ⚠️ **Not a public route**, unlike the legacy ``/archives/{id}/thumbnail`` and
    ``/library/files/{id}/thumbnail`` beside it. A picture is content: it is
    behind the same ``queue:read_all`` / ``queue:read_own`` split ``GET /queue/``
    uses and answers 404 — never 403 — on a row the caller may not see, so a
    refusal does not confirm that an id exists. That is also why the frontend
    fetches it with the bearer token and hands an object URL to ``<img>`` instead
    of threading a camera stream token through the URL: the stream token carries
    no identity at all, so it could not express "their row, not yours".

    ⚠️ **The bytes are read under a GC pin** (§9), so the collector cannot unlink
    the object between the decision and the read, and off the event loop, because
    it is a ZIP read (S10).

    404 covers every kind of "no picture", and each is an ordinary state rather
    than a failure: a legacy row, a raw G-code source, a plate the slicer never
    rendered, a ``broken`` blob whose bytes are gone. The caller draws its empty
    state; nothing here retries and nothing 500s.
    """
    current_user, can_read_all = auth_result
    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if (
        current_user is not None
        and not can_read_all
        and (item.created_by_id is None or item.created_by_id != current_user.id)
    ):
        raise HTTPException(404, "Queue item not found")

    # The ONE way a job's source is resolved (§7): never a path rebuilt from an
    # original row, which is exactly the trap this feature closed.
    descriptor = await item_descriptor(db, item)
    if descriptor is None or descriptor.queue_source_id is None:
        raise HTTPException(404, "No plate picture for this job")

    plate_id = item.plate_id or descriptor.plate_fallback
    async with queue_sources.pin(descriptor.queue_source_id):
        data = await asyncio.to_thread(_read_plate_picture, descriptor.path, plate_id)
    if data is None:
        raise HTTPException(404, "No plate picture for this job")

    # A content-addressed object never changes, so (hash, plate) is an exact
    # validator and the browser may keep the picture for as long as it likes.
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": f'"{descriptor.sha256}-{plate_id}"',
        },
    )


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
        new_queue = (
            await db.execute(select(PrinterQueue).where(PrinterQueue.id == update_data["queue_id"]))
        ).scalar_one_or_none()
        if not new_queue:
            raise HTTPException(400, "Queue not found")

        # Cross-model safety gate (#2578) — moving an item to another printer's
        # queue can't drop a G-code 3MF onto a model it wasn't sliced for.
        sliced_for = None
        if item.archive_id:
            sliced_for = (
                await db.execute(select(PrintArchive.sliced_for_model).where(PrintArchive.id == item.archive_id))
            ).scalar_one_or_none()
        elif item.library_file_id:
            lib = (
                await db.execute(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
            ).scalar_one_or_none()
            if lib and lib.file_metadata:
                sliced_for = lib.file_metadata.get("sliced_for_model")
        if sliced_for and new_queue.printer_id is not None:
            from backend.app.models.printer import Printer

            printer_model = (
                await db.execute(select(Printer.model).where(Printer.id == new_queue.printer_id))
            ).scalar_one_or_none()
            if not is_gcode_compatible(sliced_for, printer_model):
                raise HTTPException(
                    400,
                    f"File was sliced for {sliced_for} and cannot be dispatched to a {printer_model} printer",
                )

    update_data = await routing_update(db, item, update_data)

    # Serialize ams_mapping to JSON for TEXT column storage
    if "ams_mapping" in update_data:
        update_data["ams_mapping"] = json.dumps(update_data["ams_mapping"]) if update_data["ams_mapping"] else None

    # Serialize H2C rack-swap nozzle pick (#1780) to JSON for TEXT column
    # storage; same opaque-blob convention as ams_mapping above.
    if "nozzle_mapping" in update_data:
        update_data["nozzle_mapping"] = (
            json.dumps(update_data["nozzle_mapping"]) if update_data["nozzle_mapping"] else None
        )

    # swap_macro_events is stored as a JSON-encoded TEXT column.
    if "swap_macro_events" in update_data:
        events = update_data["swap_macro_events"]
        update_data["swap_macro_events"] = json.dumps(events) if events else None
    if "selected_macro_ids" in update_data:
        ids = update_data["selected_macro_ids"]
        update_data["selected_macro_ids"] = json.dumps(ids) if ids is not None else None

    for field, value in update_data.items():
        if field in _CALI_MODE_FIELDS:
            _set_calibration_mode(item, field, value)
        else:
            setattr(item, field, value)

    await db.commit()

    # Re-query with full eager loading (queue→printer chain)
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
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

    from backend.app.services.queue_counters import detach_print_queue_refs, update_queue_counters

    await detach_print_queue_refs(db, [item.id])
    await db.delete(item)
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
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_UPDATE_ALL,
            Permission.QUEUE_UPDATE_OWN,
        )
    ),
):
    """Stop an actively printing queue item.

    Ownership-scoped (upstream #1625-followup): QUEUE_UPDATE_OWN holders can stop
    their own items, QUEUE_UPDATE_ALL any item — mirrors /cancel. Pre-fix this
    required QUEUE_UPDATE_ALL, so an operator holding only _OWN got a 403 when
    calling the queue-stop API for their own item. Ownerless items require _ALL:
    stop is destructive and an _OWN holder can't claim it the way /start does.
    """
    from backend.app.services.printer_manager import printer_manager

    user, can_modify_all = auth_result

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    # Ownership check — mirrors /cancel. Ownerless items require _ALL.
    if not can_modify_all and user is not None:
        if item.created_by_id is None or item.created_by_id != user.id:
            raise HTTPException(403, "You can only stop your own queue items")

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

    # ⚠️ Signal the dispatcher BEFORE the stop command. A stop only makes sense
    # once a print is running; during the preheat stage there is nothing to
    # stop, and the dispatch coroutine — which only ever sees this through its
    # own cancel-check — would otherwise keep the heaters on for the rest of
    # the wait and soak while holding the printer claimed.
    try:
        from backend.app.services.background_dispatch import background_dispatch

        background_dispatch.cancel_dispatch_for_queue_item(item_id)
    except Exception as exc:  # noqa: BLE001 - the stop below matters more
        logger.warning("Could not signal the dispatcher for stopped queue item %s: %s", item_id, exc)

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

    # Reconcile the linked archive when the printer is offline (#2603). When the
    # stop command reaches the printer it later reports the stop over MQTT and
    # on_print_complete flips the archive to cancelled. When the printer is
    # offline no such event ever arrives, so the archive would stay "printing"
    # forever (queue row cancelled, archive still printing). Close it out here,
    # mirroring the MQTT cancelled-branch. Only touch a still-"printing" archive
    # so a real completion that raced in is never overwritten.
    if not stop_sent and item.archive_id:
        archive = await db.get(PrintArchive, item.archive_id)
        if archive and archive.status == "printing":
            archive.status = "cancelled"
            archive.completed_at = datetime.now(timezone.utc)
            archive.failure_reason = "Stopped by user (printer was offline)"

    # User-initiated stop pauses the queue (not idle) so the operator
    # explicitly resumes after inspecting the printer / dealing with the
    # reason they stopped. Mirrors ``_cancel_item`` in the scheduler and
    # the runtime ``on_print_complete`` cancelled-branch (main.py) so all
    # three cancel paths behave the same way.
    from backend.app.services.queue_counters import set_queue_paused, update_queue_counters

    await set_queue_paused(db, item.queue_id, paused_item_id=item.id)
    await update_queue_counters(db, item.queue_id)
    await db.commit()

    logger.info("Stopped printing queue item %s (stop command sent: %s)", item_id, stop_sent)

    # Schedule power-off if the queue item opted in. Delegates to the smart-plug
    # manager so the off honours each plug's configured strategy (time delay or
    # temperature threshold), is cancelled if the printer starts printing again,
    # and never cuts power on a loaded print (#1890). Previously an inline block
    # hardcoded a 50°C / 600s cooldown wait and powered off on the timeout
    # regardless of print state.
    if auto_off_after:
        from backend.app.services.smart_plug_manager import smart_plug_manager

        try:
            await smart_plug_manager.schedule_off_after_queue_job(printer_id, db)
        except Exception as e:
            logger.warning("Auto-off: Failed to schedule power-off for printer %s: %s", printer_id, e)

    return {"message": "Print stopped" if stop_sent else "Queue item cancelled (printer was offline)"}


@router.post("/{item_id}/start")
async def start_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_UPDATE_ALL,
            Permission.QUEUE_UPDATE_OWN,
        )
    ),
):
    """Manually start a staged (manual_start) queue item.

    Ownership-scoped (upstream #1625-followup): QUEUE_UPDATE_OWN holders can
    start their own items + claim NULL-owner items (VP-uploaded items arrive
    unattributed, #1670); QUEUE_UPDATE_ALL can start any item. Pre-fix this
    required QUEUE_UPDATE_OWN with no ownership check, so _OWN holders could
    start anyone's queue items via direct API.

    This clears the manual_start flag so the scheduler will pick it up,
    or starts immediately if the printer is ready.
    """
    user, can_modify_all = auth_result

    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
        )
        .where(PrintQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")

    # Ownership check — softer than /stop because /start is the entry point for
    # #1670's VP-import flow: a NULL-owner item is claimable by the first _OWN
    # holder who clicks ▶ (credited as owner below). A different owner → 403.
    if not can_modify_all and user is not None:
        if item.created_by_id is not None and item.created_by_id != user.id:
            raise HTTPException(403, "You can only start your own queue items")

    if item.status != "pending":
        raise HTTPException(400, f"Can only start pending items, current status: '{item.status}'")

    # Clear manual_start flag so scheduler picks it up
    item.manual_start = False
    # Attribute the print to the operator who started it when the item has no
    # owner yet (upstream #1670). An item added through the UI already carries
    # its uploader in created_by_id — preserve that; only fill the gap for
    # otherwise-unattributed items so the dispatched archive (our print log)
    # isn't left with an empty User column when auth is on.
    if item.created_by_id is None and user is not None:
        item.created_by_id = user.id
    await db.commit()

    # Re-query with full eager loading (queue→printer chain)
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
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

    ⚠️ m173: a clone becomes a second owner of the original's captured bytes, so it
    is refused when those bytes are not printable — with the capture taxonomy's own
    status, mapped here and nowhere else (spec §6).
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import clone_batch, clone_item

    src = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not src:
        raise HTTPException(404, "Queue item not found")

    try:
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
    except QueueSourceError as exc:
        raise refusal(exc) from exc

    # Re-fetch with full eager loading for response.
    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
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


async def _acknowledge_blocking_failure(db: AsyncSession, queue_id: int) -> None:
    """Retire the failure that the ``require_previous_success`` gate is reading.

    Deliberately mirrors ``PrintScheduler.previous_print_succeeded``: same
    lookback, same ordering. It marks the newest finished row only when that row
    is the failure — if a print has succeeded since, nothing is gating and there
    is nothing to acknowledge.
    """
    blocking = (
        await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.queue_id == queue_id)
            .where(PrintQueueItem.status.in_(["completed", "failed", "cancelled"]))
            .where(PrintQueueItem.gate_acknowledged.is_(False))
            .order_by(PrintQueueItem.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if blocking is not None and blocking.status == "failed":
        blocking.gate_acknowledged = True


@router.post("/{item_id}/unskip")
async def unskip_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Revert a skipped item back to pending, appended to end of queue.

    Doubles as the release for the ``require_previous_success`` gate (m116).
    An item skipped because the previous print failed would be skipped again on
    the very next tick — the failure it looked at is still the newest one — so
    putting it back in the queue has to mean "I have dealt with that failure".
    The blocking row is marked ``gate_acknowledged``, which drops it out of the
    lookback for every item behind it too, not just this one.

    ⚠️ m173: putting a row back into the queue re-arms it on the bytes it already
    owns — never a new read of its original (spec §7, A08). The blob is therefore
    asked for under the storage guard and the re-arm is written under it, and a blob
    that is not ``ready`` leaves the row skipped rather than pending-and-doomed.
    """
    from backend.app.services.queue_counters import update_queue_counters

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "skipped":
        raise HTTPException(400, f"Only skipped items can be unskipped, current status: '{item.status}'")

    from backend.app.services.filament_policy import restore_routing_source

    try:
        async with reusing_sources(db, [item.queue_source_id]):
            if item.require_previous_success:
                await _acknowledge_blocking_failure(db, item.queue_id)

            max_pos = (
                await db.execute(
                    select(func.max(PrintQueueItem.position))
                    .where(PrintQueueItem.queue_id == item.queue_id)
                    .where(PrintQueueItem.status == "pending")
                )
            ).scalar() or 0
            restore_routing_source(item)
            item.status = "pending"
            item.position = max_pos + 1
            await update_queue_counters(db, item.queue_id)
            await db.commit()
    except QueueSourceError as exc:
        raise refusal(exc) from exc
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
    """Put a terminal-state item back into pending status, appended to end of queue.

    Accepts both ``failed`` (auto-dispatch error) and ``cancelled``
    (user-initiated cancel during dispatch) items. The "retry"/"restart"
    distinction is presentation-level only — backend state machine is the
    same: terminal → pending, error_message cleared, position appended.

    ⚠️ m173: a retry re-runs the bytes this row already owns — the failure it is
    retrying may well have been the original going away, so nothing here reads it
    (spec §7, A08). The blob is asked for under the storage guard and the re-arm is
    written under it; bytes that are not ``ready`` leave the row terminal, where the
    operator can see it, rather than pending and certain to fail again.
    """
    from backend.app.services.queue_counters import update_queue_counters

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status not in ("failed", "cancelled"):
        raise HTTPException(400, f"Only failed or cancelled items can be retried, current status: '{item.status}'")

    from backend.app.services.filament_policy import restore_routing_source

    try:
        async with reusing_sources(db, [item.queue_source_id]):
            max_pos = (
                await db.execute(
                    select(func.max(PrintQueueItem.position))
                    .where(PrintQueueItem.queue_id == item.queue_id)
                    .where(PrintQueueItem.status == "pending")
                )
            ).scalar() or 0
            restore_routing_source(item)
            item.status = "pending"
            item.position = max_pos + 1
            item.error_message = None
            item.completed_at = None
            await update_queue_counters(db, item.queue_id)
            await db.commit()
    except QueueSourceError as exc:
        raise refusal(exc) from exc

    result = await db.execute(
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
        )
        .where(PrintQueueItem.id == item_id)
    )
    return _enrich_response(result.scalar_one())


# ============================================================================
# Batch-level operations
# ============================================================================


@router.post("/batch")
async def group_items_into_batch(
    data: PrintQueueBatchCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Group 2+ pending queue items into a new shared batch (upstream #1743
    "Group as batch"). All items must be pending and in the same printer queue —
    a batch is a per-queue block. Returns the generated batch_id."""
    if len(set(data.item_ids)) < 2:
        raise HTTPException(400, "A batch needs at least 2 items")

    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(data.item_ids)))
    items = result.scalars().all()
    if len(items) != len(set(data.item_ids)):
        raise HTTPException(404, "One or more queue items not found")
    if any(it.status != "pending" for it in items):
        raise HTTPException(400, "Only pending items can be grouped into a batch")
    queue_ids = {it.queue_id for it in items}
    if len(queue_ids) != 1:
        raise HTTPException(400, "All items must be in the same printer queue")

    batch_id = str(uuid.uuid4())
    for it in items:
        it.batch_id = batch_id

    from backend.app.services.queue_counters import update_queue_counters

    await update_queue_counters(db, next(iter(queue_ids)))
    await db.commit()
    logger.info("Grouped %s queue items into batch %s", len(items), batch_id)
    return {"batch_id": batch_id, "count": len(items)}


@router.post("/batch/{batch_id}/ungroup")
async def ungroup_batch(
    batch_id: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Disband a batch (upstream #1743): clear batch_id from its still-in-queue
    members (pending + printing). Completed/cancelled history keeps its grouping."""
    result = await db.execute(
        select(PrintQueueItem).where(
            PrintQueueItem.batch_id == batch_id,
            PrintQueueItem.status.in_(["pending", "printing"]),
        )
    )
    items = result.scalars().all()
    if not items:
        raise HTTPException(404, "No active items in this batch")
    queue_id = items[0].queue_id
    for it in items:
        it.batch_id = None

    from backend.app.services.queue_counters import update_queue_counters

    await update_queue_counters(db, queue_id)
    await db.commit()
    logger.info("Ungrouped batch %s (%s items)", batch_id, len(items))
    return {"ungrouped": len(items), "batch_id": batch_id}


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
    # Serialize H2C rack-swap nozzle pick (#1780); mirrors ams_mapping above.
    if "nozzle_mapping" in update_data:
        update_data["nozzle_mapping"] = (
            json.dumps(update_data["nozzle_mapping"]) if update_data["nozzle_mapping"] else None
        )
    if "swap_macro_events" in update_data:
        events = update_data["swap_macro_events"]
        update_data["swap_macro_events"] = json.dumps(events) if events else None
    if "selected_macro_ids" in update_data:
        ids = update_data["selected_macro_ids"]
        update_data["selected_macro_ids"] = json.dumps(ids) if ids is not None else None

    for item in pending:
        item_update = await routing_update(db, item, update_data)
        if isinstance(item_update.get("ams_mapping"), list):
            item_update["ams_mapping"] = json.dumps(item_update["ams_mapping"])
        for field, value in item_update.items():
            if field in _CALI_MODE_FIELDS:
                _set_calibration_mode(item, field, value)
            else:
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

    ⚠️ m173: the SECOND door onto ``clone_item`` / ``clone_batch``, and therefore
    the second that has to answer the capture taxonomy. Both services refuse a
    blob that is not ``ready``, and an unmapped refusal here leaves as a bare 500
    — on the operator's screen the difference between "the file is unreadable" and
    "BamDude is broken", and the frontend branches on ``detail.code``.
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import clone_batch, clone_item, get_batch_pending_items

    pending = await get_batch_pending_items(db, batch_id)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    queue_id = pending[0].queue_id

    try:
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
    except QueueSourceError as exc:
        raise refusal(exc) from exc
    return {
        "cloned": len(clones),
        "scope": "batch",
        "source_batch_id": batch_id,
        "new_batch_id": clones[0].batch_id,
    }
