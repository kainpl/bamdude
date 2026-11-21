"""API routes for print queue management."""

import asyncio
import json
import logging
import uuid
import zipfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, inspect as sa_inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, load_only, selectinload

from backend.app.api.routes._workshop_rights import ensure_may_file_future
from backend.app.core.api_key_scope import key_printer_scope
from backend.app.core.auth import (
    RequestCredentials,
    RequirePermission,
    request_credentials,
    require_ownership_permission,
    require_permission,
)
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import FORMAT_GCODE, STATE_READY, QueueSource
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import derive_mode, normalize_mode
from backend.app.schemas.farm_forecast import FarmForecastOut
from backend.app.schemas.print_queue import (
    PrintQueueBatchCreate,
    PrintQueueBulkDelete,
    PrintQueueBulkDeleteResponse,
    PrintQueueBulkRetry,
    PrintQueueBulkUpdate,
    PrintQueueBulkUpdateResponse,
    PrintQueueItemCreate,
    PrintQueueItemResponse,
    PrintQueueItemUpdate,
    PrintQueueNextBatchCreate,
    PrintQueueReorder,
    QueueAutoTransferApply,
    QueueCopySourceProfile,
    QueueIssuesResponse,
    QueueLeftoverOut,
    QueueLeftoverSkipped,
    QueueLeftoversResponse,
    QueueLeftoversRetire,
    QueueLeftoversRetireResult,
    QueueSummaryGroup,
    QueueSummaryResponse,
)
from backend.app.services import farm_forecast, queue_sources, swap_compatibility
from backend.app.services.filament_intake import (
    item_descriptor,
    item_source,
    loaded_descriptor,
    require_source_requirements,
    routing_detail,
    source_display_filename,
)
from backend.app.services.filament_policy import decode
from backend.app.services.filament_policy_write import routing_update
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_add import add_items_to_printer_queue, add_next_block_to_printer_queue
from backend.app.services.queue_add_effects import announce_printer_queue_add
from backend.app.services.queue_edit_apply import CheckedFacts, apply_edits, checked_facts, plan_edit
from backend.app.services.queue_row_basis import row_basis
from backend.app.services.queue_source_capture import refusal, reusing_sources, swap_request
from backend.app.services.queue_source_descriptor import source_storage_state
from backend.app.services.queue_sources import QueueSourceError
from backend.app.services.queue_times import plate_metadata_cached, plate_metadata_for_row, plate_picture_for_row
from backend.app.services.source_io import SourceUnavailable
from backend.app.utils.failure_reasons import USER_CANCELLED
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.threemf_tools import plate_picture_entry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue", tags=["queue"])


# The three tri-state calibration fields. Each has a legacy bool column
# (authoritative for off/on — read by the dispatcher + secondary sites) and a
# nullable ``*_mode`` column that carries the extra 'auto' state.
_CALI_MODE_FIELDS = ("bed_levelling", "flow_cali", "nozzle_offset_cali")


async def _item_file_model(
    db: AsyncSession, item: PrintQueueItem, plate_id: int | None, cache: PrintRequirementsCache
) -> str | None:
    """Read the model from this row's own bytes, including a changed plate."""
    descriptor = await item_descriptor(db, item)
    if descriptor is not None:
        try:
            source_display_filename(descriptor)
        except SourceUnavailable as exc:
            raise HTTPException(422, routing_detail("source_unreadable")) from exc
        if descriptor.format == FORMAT_GCODE:
            return None
        req = await require_source_requirements(cache, plate_id=plate_id, descriptor=descriptor)
    else:
        archive, library = await item_source(db, item)
        req = await require_source_requirements(
            cache, archive=archive, library_file=library, plate_id=plate_id, allow_raw_gcode=True
        )
    return req.model if req else None


_SWAP_FIELDS = frozenset({"execute_swap_macros", "swap_macro_events"})


def _end_capture_cooldowns(item_ids) -> None:
    """An explicit edit, delete or Retry ends a job's busy-spool wait at once (swap-source-lifecycle spec r4 §6)."""
    from backend.app.services.print_scheduler import scheduler

    for item_id in item_ids:
        scheduler.clear_capture_cooldown(item_id)


def _queue_edit_conflict() -> HTTPException:
    return HTTPException(
        409,
        detail={
            "error": "queue_item_changed",
            "message": "This job changed or started while it was being edited. Nothing was saved; refresh and try again.",
        },
    )


async def _swap_update(
    db: AsyncSession, item: PrintQueueItem, update: dict, target_queue: PrinterQueue | None, snapshot: CheckedFacts
) -> dict:
    """The job's swap columns after an edit, from its OWN captured declaration (swap-source-lifecycle spec r4 §8).

    Asked only when the edit touches the swap fields or moves the job to another printer's queue: a baked source
    stays muted, an edit of a plain one's request is stored under the printer's swap mode, an opt-out is never
    raised, and an unknown source asked for our macros is a 409. A move is not an edit of the request: the job
    keeps what was asked for the way back -- dispatch mutes it on a printer without swap mode -- and only the
    target may refuse it. Never the library or archive row's current value. Returns the columns to write --
    ``swap_macro_events`` as its stored JSON -- or ``{}`` for nothing to write.
    """
    moves = target_queue is not None and target_queue.id != item.queue_id
    if not (_SWAP_FIELDS & update.keys() or moves):
        return {}
    execute = (
        update["execute_swap_macros"] if update.get("execute_swap_macros") is not None else item.execute_swap_macros
    )
    if "swap_macro_events" in update:
        events = update["swap_macro_events"]
        events = json.loads(events) if isinstance(events, str) else events
    else:
        events = json.loads(item.swap_macro_events) if item.swap_macro_events else None
    # The printer as the edit's checks read it (G03 B A6).
    queue_id = target_queue.id if moves else item.queue_id
    printer = snapshot.printer(queue_id)
    descriptor = await item_descriptor(db, item)
    fact = descriptor.swap_fact.value if descriptor is not None else None
    execute, events = swap_request(
        fact,
        execute=execute,
        events=events,
        printer_swap_mode=bool(printer is not None and printer.swap_mode_enabled),
    )
    if moves and not _SWAP_FIELDS & update.keys() and fact is not True:
        return {}
    return {"execute_swap_macros": execute, "swap_macro_events": json.dumps(events) if events else None}


async def _check_item_model_for_queue(
    db: AsyncSession,
    item: PrintQueueItem,
    queue: PrinterQueue,
    plate_id: int | None,
    cache: PrintRequirementsCache,
    printer_model: str | None,
) -> None:
    if queue.printer_id is None:
        return
    sliced_for = await _item_file_model(db, item, plate_id, cache)
    target = printer_manager.effective_model_for(queue.printer_id, printer_model)
    if model_compatibility(sliced_for, target) == "incompatible":
        raise HTTPException(
            400, f"File was sliced for {sliced_for} and cannot be dispatched to a {printer_model} printer"
        )


def _set_item_field(item: PrintQueueItem, field: str, value) -> None:
    """One validated value onto the row an edit applies to (A6)."""
    if field in _CALI_MODE_FIELDS:
        _set_calibration_mode(item, field, value)
    else:
        setattr(item, field, value)


def _set_calibration_mode(item: PrintQueueItem, field: str, value) -> None:
    """Store a tri-state calibration value on ``item``: the legacy bool mirror
    (``field``) plus the ``field_mode`` column. ``value`` is a CalibrationMode
    string (the schema coercer already normalised any legacy bool), but a raw
    bool is tolerated defensively."""
    mode = normalize_mode(value)
    setattr(item, field, mode == "on")
    setattr(item, f"{field}_mode", mode)


def queue_item_load_options() -> tuple:
    """What ``_enrich_response`` reads, loaded up front — the queue list and the
    order's queue section (``GET /projects/{id}/queue``) build their rows from the
    same set."""
    return (
        selectinload(PrintQueueItem.archive),
        selectinload(PrintQueueItem.queue_source),
        selectinload(PrintQueueItem.queue)
        .selectinload(PrinterQueue.printer)
        .options(
            # The two gate columns answer ``plate_answer_required`` (spec plate-gate-owner-requeue A4).
            load_only(
                Printer.id,
                Printer.name,
                Printer.awaiting_plate_clear,
                Printer.awaiting_plate_clear_archive_id,
            ),
            lazyload(Printer.tags),
            lazyload(Printer.location),
        ),
        selectinload(PrintQueueItem.library_file),
        selectinload(PrintQueueItem.created_by),
        selectinload(PrintQueueItem.project),
    )


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

    # The operator's rack-position pick (upstream #1784), keyed by filament
    # group. Sent parsed so the print dialog can show which hotend each group
    # will use when the item is edited.
    nozzle_rack_choice_parsed = None
    if item.nozzle_rack_choice:
        try:
            nozzle_rack_choice_parsed = json.loads(item.nozzle_rack_choice)
        except json.JSONDecodeError:
            nozzle_rack_choice_parsed = None

    # Create response with parsed ams_mapping
    item_dict = {
        "returned_receipt_identity": item.returned_receipt_identity,
        "return_ordinal": getattr(item, "_return_ordinal", None),
        "return_refusal_code": getattr(item, "_return_refusal_code", None),
        "auto_eject": item.auto_eject,
        "auto_eject_settings": item.auto_eject_settings or {},
        "id": item.id,
        "queue_id": item.queue_id,
        "printer_id": item.printer_id,  # convenience property from queue
        "project_id": item.project_id,
        "project_line_id": item.project_line_id,
        "waiting_reason": item.waiting_reason,
        "waiting_reason_code": item.waiting_reason_code,
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
        "nozzle_rack_choice": nozzle_rack_choice_parsed,
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
        response.project_status = item.project.status if item.project else None
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
        intent = decode(item.filament_routing, {})
        # Captured bytes outrank original metadata even when the latter was
        # edited or the source row has since disappeared.
        response.sliced_for_model = intent.get("file_model") if isinstance(intent, dict) else None
        # Missing prediction on the selected captured plate is unknown; an
        # original file/another plate cannot supply its display estimate.
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
        printer = item.queue.printer
        response.printer_name = printer.name
        # Only when the reader loaded the gate (``queue_item_load_options`` does): an
        # unloaded column here would be a MissingGreenlet, not a lazy read. Retry asks
        # the database itself; this flag only tells the card which button to offer.
        if item.archive_id is not None and not (
            {"awaiting_plate_clear", "awaiting_plate_clear_archive_id"} & sa_inspect(printer).unloaded
        ):
            response.plate_answer_required = bool(
                printer.awaiting_plate_clear and printer.awaiting_plate_clear_archive_id == item.archive_id
            )
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


@router.get("/summary", response_model=QueueSummaryResponse)
async def queue_summary(
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(Permission.QUEUE_READ_ALL, Permission.QUEUE_READ_OWN)
    ),
):
    """Small badge/issue counts from real visible queue rows, not archive history."""
    user, can_read_all = auth_result
    stmt = (
        select(PrintQueueItem.queue_id, PrinterQueue.printer_id, PrintQueueItem.status, func.count(PrintQueueItem.id))
        .outerjoin(PrinterQueue, PrinterQueue.id == PrintQueueItem.queue_id)
        .where(PrintQueueItem.status.in_(("pending", "failed", "skipped", "cancelled")))
        .group_by(PrintQueueItem.queue_id, PrinterQueue.printer_id, PrintQueueItem.status)
        .order_by(PrintQueueItem.queue_id)
    )
    if user is not None and not can_read_all:
        stmt = stmt.where(PrintQueueItem.created_by_id == user.id)

    groups: dict[int, QueueSummaryGroup] = {}
    for queue_id, printer_id, status, count in (await db.execute(stmt)).all():
        group = groups.setdefault(queue_id, QueueSummaryGroup(queue_id=queue_id, printer_id=printer_id))
        setattr(group, f"{status}_count", int(count))
    return QueueSummaryResponse(
        pending_count=sum(group.pending_count for group in groups.values()), groups=list(groups.values())
    )


@router.get("/issues", response_model=QueueIssuesResponse)
async def queue_issues(
    queue_id: int = Query(..., description="Printer queue ID"),
    cursor: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(Permission.QUEUE_READ_ALL, Permission.QUEUE_READ_OWN)
    ),
):
    """Bounded, id-ordered issue details; virtual printing rows never appear."""
    user, can_read_all = auth_result
    stmt = (
        select(PrintQueueItem)
        .options(
            selectinload(PrintQueueItem.archive),
            selectinload(PrintQueueItem.queue_source),
            selectinload(PrintQueueItem.queue).selectinload(PrinterQueue.printer),
            selectinload(PrintQueueItem.library_file),
            selectinload(PrintQueueItem.created_by),
            selectinload(PrintQueueItem.project),
        )
        .where(PrintQueueItem.queue_id == queue_id)
        .where(PrintQueueItem.status.in_(("failed", "skipped", "cancelled")))
        .order_by(PrintQueueItem.id)
        .limit(limit + 1)
    )
    if cursor is not None:
        stmt = stmt.where(PrintQueueItem.id > cursor)
    if user is not None and not can_read_all:
        stmt = stmt.where(PrintQueueItem.created_by_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    from backend.app.services.auto_queue_return_views import describe_returns

    await describe_returns(db, rows[:limit])
    return QueueIssuesResponse(
        items=[_enrich_response(item) for item in rows[:limit]],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get("/leftovers", response_model=QueueLeftoversResponse)
async def list_queue_leftovers(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_READ_ALL),
):
    """Rows nothing will ever clean up: superseded finished rows and orphan router assignments.

    A key with a printer list sees its printers' rows only, and no orphan assignment (it has no printer).
    """
    from backend.app.services import queue_leftovers

    found = await queue_leftovers.find(db, printer_ids=key_printer_scope(request))
    return QueueLeftoversResponse(
        items=[
            QueueLeftoverOut(
                kind=leftover.kind,
                item_id=leftover.item_id,
                auto_item_id=leftover.auto_item_id,
                printer_id=leftover.printer_id,
                printer_name=leftover.printer_name,
                archive_id=leftover.archive_id,
                name=leftover.name,
                completed_at=leftover.completed_at,
                hold_reason=leftover.hold_reason,
                retirable=leftover.keep_reason is None,
                keep_reason=leftover.keep_reason,
            )
            for leftover in found
        ]
    )


@router.post("/leftovers/retire", response_model=QueueLeftoversRetireResult)
async def retire_queue_leftovers(
    data: QueueLeftoversRetire,
    request: Request,
    _: User | None = RequirePermission(Permission.QUEUE_DELETE_ALL),
):
    """Retire the named leftovers the policy still allows; the rest come back with the reason.

    ``item_ids`` are read by the API-key printer-scope gate like any queue item list. Orphan assignments have
    no printer, so a key with a printer list does not retire them: they come back as ``out_of_scope``.
    """
    from backend.app.services import queue_leftovers

    scoped = key_printer_scope(request) is not None
    result = await queue_leftovers.retire(item_ids=data.item_ids, auto_item_ids=() if scoped else data.orphan_ids)
    skipped = [
        QueueLeftoverSkipped(kind=s.kind, item_id=s.item_id, auto_item_id=s.auto_item_id, reason=s.reason)
        for s in result.skipped
    ]
    if scoped:
        skipped += [
            QueueLeftoverSkipped(kind="orphan_assignment", auto_item_id=auto_id, reason="out_of_scope")
            for auto_id in sorted(set(data.orphan_ids))
        ]
    return QueueLeftoversRetireResult(
        retired_item_ids=result.retired_items, retired_orphan_ids=result.retired_autos, skipped=skipped
    )


@router.get("/", response_model=list[PrintQueueItemResponse])
async def list_queue(
    request: Request,
    queue_id: int | None = Query(None, description="Filter by printer queue"),
    status: str | None = Query(
        None, description="Filter by status; several are comma-separated (pending,printing), as on /auto-queue/"
    ),
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_READ_ALL,
            Permission.QUEUE_READ_OWN,
        )
    ),
):
    """List all queue items, optionally filtered by queue or status.

    ``status`` takes one value or several, comma-separated (G03 D9): ``pending,printing`` used to be
    compared as one literal and answered nothing, while the AutoQueue list split it. The rows come in
    the order the claim reads them -- position, then id, inside each queue (G03 E8).
    """
    user, can_read_all = auth_result
    statuses = [value.strip() for value in status.split(",") if value.strip()] if status else []
    query = (
        select(PrintQueueItem)
        .options(*queue_item_load_options())
        .order_by(PrintQueueItem.queue_id, PrintQueueItem.position, PrintQueueItem.id)
    )
    if user is not None and not can_read_all:
        query = query.where(PrintQueueItem.created_by_id == user.id)
    key_scope = key_printer_scope(request)
    if key_scope is not None:
        query = query.where(PrintQueueItem.queue_id.in_(key_scope))

    if queue_id is not None:
        query = query.where(PrintQueueItem.queue_id == queue_id)
    if statuses:
        query = query.where(PrintQueueItem.status.in_(statuses))

    result = await db.execute(query)
    items = result.scalars().all()
    from backend.app.services.auto_queue_return_views import describe_returns

    await describe_returns(db, items)
    enriched = [_enrich_response(item) for item in items]

    # Augment with virtual current-print items for printers whose queue
    # doesn't have a printing item but whose printer is actively busy
    # (external / direct-dispatch prints).  Skipped when the caller
    # filtered by statuses that leave ``printing`` out.
    if not statuses or "printing" in statuses:
        from backend.app.services.queue_virtual import build_virtual_current_prints

        virtual = await build_virtual_current_prints(
            db,
            queue_id=queue_id,
            viewer_id=user.id if user is not None and not can_read_all else None,
        )
        # A key restricted to some printers sees only their current prints
        # (inv-api-key-printer-scope-in-the-gate); a queue's id is its printer's.
        if key_scope is not None:
            virtual = [row for row in virtual if row["queue_id"] in key_scope]
        enriched = virtual + enriched

    return enriched


async def _ensure_may_queue_archives(
    db: AsyncSession, creds: RequestCredentials, current_user: User | None, archive_ids: list[int | None]
) -> None:
    """A queue item made from an archive is a reprint of it: beside ``queue:create`` it asks
    what ``POST /archives/{id}/reprint`` asks — the archive visible and not in the trash (404)
    and the reprint right for it (403). A copy of a queue row reads no archive and asks nothing."""
    from backend.app.api.routes.archives import ensure_may_print_from_archive

    for archive_id in sorted({i for i in archive_ids if i is not None}):
        await ensure_may_print_from_archive(creds, await db.get(PrintArchive, archive_id), current_user)


@router.post("/", response_model=PrintQueueItemResponse)
async def add_to_queue(
    data: PrintQueueItemCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Add an item to the print queue. Under an order it asks the Workshop's filing right
    too (``Fф``, WS-13 E13 O10); the order's own checks are the writer's."""
    if data.project_id is not None or data.project_line_id is not None:
        await ensure_may_file_future(creds)
    await _ensure_may_queue_archives(db, creds, current_user, [data.archive_id])
    # Queue creation alone may append work, but moving it in front of someone
    # else's pending jobs is a reorder operation. API keys map both permissions
    # to their existing ``can_queue`` scope; a signed-in user is checked here.
    if (
        data.enqueue_position == "next"
        and current_user is not None
        and not current_user.has_permission(Permission.QUEUE_REORDER.value)
    ):
        raise HTTPException(403, "Missing permission: queue:reorder")
    # Every gate, the advisory lock, the position and the build live in
    # ``services/queue_add`` so the file manager's bulk add cannot become a
    # second definition of what a queue item is.
    items, queue = await add_items_to_printer_queue(db, data, current_user, authority=await creds.capture())
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

    source_name = (
        f"queue source {data.source_queue_item_id}"
        if data.source_queue_item_id
        else f"archive {data.archive_id}"
        if data.archive_id
        else f"library file {data.library_file_id}"
    )
    target_desc = queue.printer.name if queue.printer else f"queue {data.queue_id}"
    logger.info("Added %s to queue for %s", source_name, target_desc)

    # The MQTT relay event and the "job added" notification -- shared with the slicer connector's fresh publications.
    await announce_printer_queue_add(db, item, queue)

    response = _enrich_response(item)
    response.created_item_ids = created_item_ids
    return response


@router.post("/next-block", response_model=list[PrintQueueItemResponse])
async def add_next_block_to_queue(
    data: PrintQueueNextBatchCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Insert several plates as one contiguous urgent block for one printer."""
    if current_user is not None and not current_user.has_permission(Permission.QUEUE_REORDER.value):
        raise HTTPException(403, "Missing permission: queue:reorder")
    if any(item.project_id is not None or item.project_line_id is not None for item in data.items):
        await ensure_may_file_future(creds)
    await _ensure_may_queue_archives(db, creds, current_user, [item.archive_id for item in data.items])
    items, _queue = await add_next_block_to_printer_queue(db, data.items, current_user, authority=await creds.capture())
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
        .where(PrintQueueItem.id.in_([item.id for item in items]))
        .order_by(PrintQueueItem.position, PrintQueueItem.id)
    )
    return [_enrich_response(item) for item in result.scalars().all()]


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

    # Fetch all items
    result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(data.item_ids)))
    items = result.scalars().all()
    bases = {item.id: row_basis(item) for item in items}  # what each row's edit is validated against (A6)

    # Validate queue_id if being changed. Every queue the rows land in is read once, before
    # the checks (A6, A25): they reach the rows that read refreshed, and the plans carry it.
    moving_to = update_data.get("queue_id")
    # An explicit destination shared by every selected job is an in-place edit.
    if moving_to is not None and items and all(item.queue_id == moving_to for item in items):
        moving_to = None
    facts = await checked_facts(db, [moving_to] if moving_to is not None else [item.queue_id for item in items])
    new_queue = None
    if moving_to is not None:
        if facts[moving_to] is None:
            raise HTTPException(400, "Queue not found")
        new_queue = facts.queue(moving_to)

    editable = [
        item
        for item in items
        if item.status == "pending"
        and (can_modify_all or item.created_by_id == user.id)
        and (new_queue is not None or facts[item.queue_id] is not None)
    ]

    # Check every row that will really move before the first mutation. A row
    # skipped for status/ownership cannot veto the entire operator action.
    printer_model = None
    if new_queue and new_queue.printer_id is not None:
        printer_model = facts[new_queue.id][3]
        cache = PrintRequirementsCache()
        for item in editable:
            if item.queue_id != new_queue.id:
                await _check_item_model_for_queue(db, item, new_queue, item.plate_id, cache, printer_model)

    # The swap columns of every row that will change, decided before the first
    # write: one unknown source asked for our macros refuses the whole action.
    swap_updates = {item.id: await _swap_update(db, item, update_data, new_queue, facts) for item in editable}

    plans = []
    for item in editable:
        item_update = await routing_update(db, item, update_data, snapshot=facts)
        item_update.update(swap_updates[item.id])
        _drop_rack_pick_on_move(item, item_update, new_queue, printer_model)
        if isinstance(item_update.get("ams_mapping"), list):
            item_update["ams_mapping"] = json.dumps(item_update["ams_mapping"])
        plans.append(
            plan_edit(
                item,
                item_update,
                basis=bases[item.id],
                target_queue_id=new_queue.id if new_queue else None,
                facts=facts,
            )
        )

    # Applied under every lane the rows and the target name (A6): a row that changed or started meanwhile is
    # skipped with the non-pending ones; both queues of a move are recounted in the transaction that moves it.
    await db.rollback()
    outcome = await apply_edits(plans, set_field=_set_item_field, all_or_nothing=False)
    updated_count = len(outcome.applied)
    skipped_count = len(items) - len(editable) + len(outcome.changed)
    _end_capture_cooldowns(outcome.applied)

    logger.info("Bulk updated %s queue items, skipped %s", updated_count, skipped_count)
    return PrintQueueBulkUpdateResponse(
        updated_count=updated_count,
        skipped_count=skipped_count,
        message=f"Updated {updated_count} items" + (f", skipped {skipped_count}" if skipped_count else ""),
    )


def _drop_rack_pick_on_move(item, changes: dict, new_queue, printer_model: str | None) -> None:
    """A job that moves to another printer leaves its rack positions (H2C) behind.

    ``nozzle_mapping`` on a nozzle-rack model names positions of the rack the job was sent to; on the
    next printer the same number is another hotend, and the dispatcher sends a stored mapping as it
    is. Cleared, the dispatcher assigns positions from the new printer's own rack. An edit that sets
    the mapping itself is left alone.

    The operator's pick, ``nozzle_rack_choice``, names positions of the OLD printer's rack too: on
    another H2C it fits by chance or refuses the print after the upload and halts that queue (G03 E6,
    upstream 2699bebc3). It goes on every move the same request does not set it in, whatever the
    target model -- a printer without a rack has no use for it either.
    """
    from backend.app.utils.printer_models import is_nozzle_rack_model

    if new_queue is None or new_queue.id == item.queue_id:
        return
    if "nozzle_rack_choice" not in changes and getattr(item, "nozzle_rack_choice", None):
        changes["nozzle_rack_choice"] = None
    if "nozzle_mapping" in changes:
        return
    if item.nozzle_mapping and is_nozzle_rack_model(printer_model):
        changes["nozzle_mapping"] = None


# The only statuses the Issues section's «Delete all» may remove. A skipped
# row is deferred live work and stays; pending/printing/completed are never
# touched whatever the caller lists.
_BULK_DELETABLE_STATUSES = ("failed", "cancelled")


async def _claim_still_deletable(db: AsyncSession, item_ids: list[int]) -> set[int]:
    """Of ``item_ids``, the rows that are STILL failed/cancelled — asked inside a write.

    The list the route filtered was read a moment earlier, and a retry can
    commit in between (failed → pending). A no-op UPDATE guarded by the status
    re-checks it at write time and holds what it matched until the commit:
    on SQLite it is the transaction's first write, so it takes the writer; on
    PostgreSQL it row-locks the matches and re-evaluates the guard against a
    concurrent commit. ``detach_print_queue_refs`` must still run BEFORE the
    delete (it reads the rows), which is why this is not a guarded DELETE.
    """
    if not item_ids:
        return set()
    result = await db.execute(
        update(PrintQueueItem)
        .where(PrintQueueItem.id.in_(item_ids), PrintQueueItem.status.in_(_BULK_DELETABLE_STATUSES))
        .values(status=PrintQueueItem.status)
        .returning(PrintQueueItem.id)
        .execution_options(synchronize_session=False)
    )
    return set(result.scalars().all())


@router.post("/bulk-delete", response_model=PrintQueueBulkDeleteResponse)
async def bulk_delete_queue_items(
    data: PrintQueueBulkDelete,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_DELETE_ALL,
            Permission.QUEUE_DELETE_OWN,
        )
    ),
):
    """Delete the listed queue rows that are still failed or cancelled.

    Mirrors ``PATCH /bulk``: inside the list nothing is refused, only skipped
    and counted — a row that changed status since the operator looked at it
    (a retry that got through), a row someone else owns when the caller has
    only ``delete_own``, an id that no longer exists. The cleanup is the single
    delete's, once per row, with the counters refreshed once per queue and one
    commit for the whole request.
    """
    user, can_modify_all = auth_result

    if not data.item_ids:
        raise HTTPException(400, "No item IDs provided")

    from backend.app.services.queue_cancel_scope import cancellation_scope

    async with cancellation_scope(data.item_ids, auto=False) as db:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(data.item_ids)))
        items = result.scalars().all()

        candidates: list[PrintQueueItem] = []
        skipped = len(set(data.item_ids)) - len(items)  # ids that are already gone
        for item in items:
            if item.status not in _BULK_DELETABLE_STATUSES:
                skipped += 1
                continue
            if not can_modify_all and (user is None or item.created_by_id != user.id):
                skipped += 1
                continue
            candidates.append(item)

        claimed = await _claim_still_deletable(db, [item.id for item in candidates])
        to_delete = [item for item in candidates if item.id in claimed]
        skipped += len(candidates) - len(to_delete)  # retried (or otherwise changed) since the read

        if to_delete:
            from backend.app.services.queue_counters import detach_print_queue_refs, update_queue_counters

            await detach_print_queue_refs(db, [item.id for item in to_delete])
            queue_ids = {item.queue_id for item in to_delete}
            for item in to_delete:
                await db.delete(item)
            for queue_id in queue_ids:
                await update_queue_counters(db, queue_id)
            await db.commit()
            _end_capture_cooldowns(item.id for item in to_delete)

        logger.info("Bulk deleted %s queue items, skipped %s", len(to_delete), skipped)
        return PrintQueueBulkDeleteResponse(
            deleted_count=len(to_delete),
            skipped_count=skipped,
            message=f"Deleted {len(to_delete)} items" + (f", skipped {skipped}" if skipped else ""),
        )


def _read_copy_source_plates(path: Path) -> list[dict]:
    """Parse one managed 3MF off the event loop for the Copy Queue dialog."""
    from backend.app.services.archive import parse_plates_from_3mf

    with zipfile.ZipFile(path, "r") as zf:
        plates = parse_plates_from_3mf(zf)
    # The existing archive/library thumbnail URLs name their mutable originals.
    # A profile must never produce one; the selected source row's own thumbnail
    # endpoint remains the only snapshot picture surface.
    return [{**plate, "thumbnail_url": None} for plate in plates]


@router.get("/{item_id}/copy-source", response_model=QueueCopySourceProfile)
async def get_queue_copy_source(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.QUEUE_READ_ALL,
            Permission.QUEUE_READ_OWN,
        )
    ),
):
    """Return the saved source profile needed to copy one queue row.

    This endpoint is deliberately queue-read scoped and refuses a broken source.
    The swap declaration is the job's own, captured with its bytes; the live
    archive or library row is not its authority. Only a snapshot from before the
    declaration was captured looks back, through the job's own foreign key and a
    matching content hash (``item_descriptor``, spec r4 §6) -- those rows may
    have been deleted since, and then the answer is unknown.
    """
    current_user, can_read_all = auth_result
    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Queue item not found")
    if (
        current_user is not None
        and not can_read_all
        and (item.created_by_id is None or item.created_by_id != current_user.id)
    ):
        raise HTTPException(404, "Queue item not found")

    descriptor = await item_descriptor(db, item)
    if descriptor is None or descriptor.queue_source_id is None:
        raise HTTPException(422, routing_detail("source_unreadable"))
    source = await db.get(QueueSource, descriptor.queue_source_id)
    if source is None or source.state != STATE_READY:
        raise HTTPException(422, routing_detail("source_unreadable"))
    try:
        filename = source_display_filename(descriptor)
    except SourceUnavailable:
        raise HTTPException(422, routing_detail("source_unreadable")) from None

    try:
        async with queue_sources.pin(descriptor.queue_source_id):
            requirements = await require_source_requirements(
                PrintRequirementsCache(),
                plate_id=item.plate_id,
                allow_raw_gcode=True,
                descriptor=descriptor,
            )
            plates = (
                []
                if descriptor.format == FORMAT_GCODE
                else await asyncio.to_thread(_read_copy_source_plates, descriptor.path)
            )
    except HTTPException:
        raise
    except (OSError, SourceUnavailable, zipfile.BadZipFile):
        raise HTTPException(422, routing_detail("source_unreadable")) from None

    return QueueCopySourceProfile(
        item_id=item.id,
        filename=filename,
        sliced_for_model=requirements.model if requirements is not None else None,
        **swap_compatibility.wire_fields(descriptor.swap_fact.value),
        plates=plates,
        is_multi_plate=len(plates) > 1,
    )


@router.get("/{item_id}/auto-transfer-preview")
async def preview_auto_transfer(
    item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.QUEUE_CREATE, Permission.QUEUE_READ),
    _: tuple = Depends(require_ownership_permission(Permission.QUEUE_DELETE_ALL, Permission.QUEUE_DELETE_OWN)),
):
    """Read-only proposal to move one never-started job to the distributor."""
    from backend.app.services import queue_auto_transfer
    from backend.app.services.swap_source_declarations import actor_for

    actor = await actor_for(request, db, user)
    return await queue_auto_transfer.preview(item_id, actor)


@router.post("/{item_id}/move-to-auto")
async def move_to_auto(
    item_id: int,
    data: QueueAutoTransferApply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.QUEUE_CREATE, Permission.QUEUE_READ),
    _: tuple = Depends(require_ownership_permission(Permission.QUEUE_DELETE_ALL, Permission.QUEUE_DELETE_OWN)),
):
    """Atomically hand this copy to AutoQueue; never republish the working plan."""
    from backend.app.services import queue_auto_transfer
    from backend.app.services.swap_source_declarations import actor_for

    actor = await actor_for(request, db, user)
    return await queue_auto_transfer.apply(item_id, actor, data)


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
    from backend.app.services.auto_queue_return_views import describe_returns

    await describe_returns(db, [item])
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
    # What this edit was validated against (G03 B, A6): applied only to the row as it is now.
    basis = row_basis(item)

    update_data = data.model_dump(exclude_unset=True)

    # Validate the destination and any newly selected plate before routing_update
    # rewrites the row. The source of a captured job is its snapshot. The target is
    # read once, before every check (A6, A25): the checks reach the header and printer
    # rows that read refreshed, and the plan carries what they judged.
    moving_to = update_data.get("queue_id")
    if moving_to == item.queue_id:
        moving_to = None
    facts = await checked_facts(db, [moving_to if moving_to is not None else item.queue_id])
    new_queue = None
    if moving_to is not None:
        if facts[moving_to] is None:
            raise HTTPException(400, "Queue not found")
        new_queue = facts.queue(moving_to)
    elif facts[item.queue_id] is None:
        raise _queue_edit_conflict()
    if "plate_id" in update_data or (new_queue and new_queue.id != item.queue_id):
        target_queue = new_queue or facts.queue(item.queue_id)
        if target_queue and target_queue.printer_id is not None:
            await _check_item_model_for_queue(
                db,
                item,
                target_queue,
                update_data.get("plate_id", item.plate_id),
                PrintRequirementsCache(),
                facts[target_queue.id][3],
            )

    swap_columns = await _swap_update(db, item, update_data, new_queue, facts)
    moved_to_model = None
    if new_queue is not None and new_queue.id != item.queue_id and new_queue.printer_id is not None:
        moved_to_model = facts[new_queue.id][3]
    update_data = await routing_update(db, item, update_data, snapshot=facts)
    update_data.pop("swap_macro_events", None)  # decided above, stored as its JSON
    update_data.update(swap_columns)
    _drop_rack_pick_on_move(item, update_data, new_queue, moved_to_model)

    # Serialize ams_mapping to JSON for TEXT column storage
    if "ams_mapping" in update_data:
        update_data["ams_mapping"] = json.dumps(update_data["ams_mapping"]) if update_data["ams_mapping"] else None

    # Serialize H2C rack-swap nozzle pick (#1780) to JSON for TEXT column
    # storage; same opaque-blob convention as ams_mapping above.
    if "nozzle_mapping" in update_data:
        update_data["nozzle_mapping"] = (
            json.dumps(update_data["nozzle_mapping"]) if update_data["nozzle_mapping"] else None
        )

    # Same Text-as-JSON convention for the rack-position pick (upstream #1784).
    # An empty object or null clears it: "assign these for me again".
    if "nozzle_rack_choice" in update_data:
        update_data["nozzle_rack_choice"] = (
            json.dumps(update_data["nozzle_rack_choice"]) if update_data["nozzle_rack_choice"] else None
        )

    if "selected_macro_ids" in update_data:
        ids = update_data["selected_macro_ids"]
        update_data["selected_macro_ids"] = json.dumps(ids) if ids is not None else None

    # Apply under the lanes of the row's queue and the target (A6): the row still pending, in the queue and with
    # the basis this validation read, the target printer as it was checked — or nothing is saved.
    plan = plan_edit(item, update_data, basis=basis, target_queue_id=update_data.get("queue_id"), facts=facts)
    await db.rollback()  # the validation's reads end before the lanes
    outcome = await apply_edits([plan], set_field=_set_item_field, all_or_nothing=True)
    if outcome.changed:
        raise _queue_edit_conflict()
    _end_capture_cooldowns([item_id])

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
        .execution_options(populate_existing=True)
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

    from backend.app.services.queue_cancel_scope import cancellation_scope

    async with cancellation_scope([item_id], auto=False) as db:
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
        _end_capture_cooldowns([item_id])

        logger.info("Deleted queue item %s", item_id)
        return {"message": "Queue item deleted"}


@router.post("/reorder")
async def reorder_queue(
    data: PrintQueueReorder,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Bulk update positions for queue items.

    Every named row's queue, ascending, under its lane (G03 B, A7): a row that left its queue or stopped being
    pending since it was read keeps its place.
    """
    from backend.app.services.queue_ops import lane_session, lock_named_rows

    wanted = {entry.id: entry.position for entry in data.items}
    read = dict(
        (
            await db.execute(select(PrintQueueItem.id, PrintQueueItem.queue_id).where(PrintQueueItem.id.in_(wanted)))
        ).all()
    )
    await db.rollback()  # the read ends before the lanes
    async with lane_session(read.values()) as wdb:
        rows = await lock_named_rows(wdb, read)
        for row_id, row in rows.items():
            if row.status != "pending" or row.queue_id != read[row_id]:
                continue
            await wdb.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == row_id,
                    PrintQueueItem.status == "pending",
                    PrintQueueItem.queue_id == read[row_id],
                )
                .values(position=wanted[row_id])
                .execution_options(synchronize_session=False)
            )
        await wdb.commit()
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

    from backend.app.services.queue_cancel_scope import cancellation_scope

    async with cancellation_scope([item_id], auto=False) as db:
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


async def _stop_unbound_attempt(*, item_id, queue_id, identity, token, archive_id, stop_sent) -> str:
    """The Stop's SQL phase for a row no run is bound to (G03 B, A16) -- ``stopped`` / ``bound`` /
    ``already_stopped`` / ``changed``.

    A fresh session under ``claim_lanes([the row's queue as read], archive_id=<its archive as read>)``; rows
    archive → job (L5). The bound run is asked again first (synchronously, nothing awaited before the CAS's
    statement). Then the compare-and-set on the very attempt the Stop read -- id, incarnation, ``printing``,
    the queue and the attempt token; on a match the offline archive close (only the archive the guard covers),
    the pause through ``halt_lane``, the counters, one commit. A lost CAS re-reads the row by id AND incarnation
    and answers by what it is now: cancelled (stopped either way), pending with the deferral proof naming this
    attempt (the very attempt a no-effect deferral sent back -- cancelled now, as the operator asked), anything
    else changed: nothing written.
    """
    from backend.app.core import database
    from backend.app.services.print_run_binding import current_print_run
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.printer_occupancy import claim_token_matches
    from backend.app.services.queue_counters import halt_lane, update_queue_counters
    from backend.app.services.queue_ops import claim_lanes

    message = "Stopped by user" if stop_sent else "Stopped by user (printer was offline)"
    async with database.async_session() as db, claim_lanes(db, [queue_id], archive_id=archive_id):
        run = current_print_run(printer_manager, queue_id)
        if (
            archive_id is not None
            and run is not None
            and run.archive_id == archive_id
            and run.queue_item_id in (None, item_id)
        ):
            return "bound"
        archive = None
        if archive_id is not None:
            archive = await db.scalar(
                select(PrintArchive)
                .where(PrintArchive.id == archive_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        now = datetime.now(timezone.utc)
        result = await db.execute(
            update(PrintQueueItem)
            .where(
                PrintQueueItem.id == item_id,
                PrintQueueItem.recovery_identity == identity,
                PrintQueueItem.status == "printing",
                PrintQueueItem.queue_id == queue_id,
                PrintQueueItem.started_at.is_not_distinct_from(token),
            )
            .values(status="cancelled", completed_at=now, error_message=message, deferred_claim_token=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            # Reconcile the linked archive when the printer is offline (#2603): no MQTT report will ever close
            # it. Only a still-"printing" archive, and only the one this Stop's guard covers.
            if not stop_sent and archive is not None and archive.status == "printing":
                archive.status = "cancelled"
                archive.completed_at = now
                # A key (upstream #2974); the offline part is the stop path, not a reason.
                archive.failure_reason = USER_CANCELLED
            # User-initiated stop pauses the queue (not idle) so the operator explicitly resumes; through the
            # one halting helper (A9).
            await halt_lane(db, queue_id, item_id=item_id, kind="paused")
            await update_queue_counters(db, queue_id)
            await db.commit()
            return "stopped"
        row = (
            await db.execute(
                select(PrintQueueItem.status, PrintQueueItem.queue_id, PrintQueueItem.deferred_claim_token).where(
                    PrintQueueItem.id == item_id, PrintQueueItem.recovery_identity == identity
                )
            )
        ).first()
        if row is not None and row.status == "cancelled":
            return "already_stopped"
        if (
            row is not None
            and row.status == "pending"
            and row.queue_id == queue_id
            and claim_token_matches(row.deferred_claim_token, token)
        ):
            proof = await db.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == item_id,
                    PrintQueueItem.recovery_identity == identity,
                    PrintQueueItem.status == "pending",
                    PrintQueueItem.queue_id == queue_id,
                    PrintQueueItem.deferred_claim_token == row.deferred_claim_token,
                )
                .values(
                    status="cancelled", completed_at=now, error_message="Stopped by user", deferred_claim_token=None
                )
                .execution_options(synchronize_session=False)
            )
            if proof.rowcount:
                await halt_lane(db, queue_id, item_id=item_id, kind="paused")
                await update_queue_counters(db, queue_id)
                await db.commit()
                return "stopped"
        return "changed"


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
    # The attempt this Stop read, as plain values (G03 B, A16): its SQL phase acts on this attempt only.
    read_identity, read_token, read_archive_id = item.recovery_identity, item.started_at, item.archive_id

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

    # Send the stop and mark the printer as user-stopped BEFORE the first
    # await, so that if the MQTT on_print_complete callback fires during the
    # db.commit() yield the "failed" status is read as "cancelled" (no spurious
    # "print failed" notification). With no bound run the row is cancelled whether
    # or not the printer could be reached, so the mark stands either way; a bound
    # run is marked only when the stop really went out (see below).
    from backend.app.services.print_control import stop_print_by_user
    from backend.app.services.print_run_binding import current_print_run

    # Read before the send: is this row's print the one the printer is running under our binding?
    run = current_print_run(printer_manager, printer_id)
    is_the_bound_run = bool(
        item.archive_id is not None
        and run is not None
        and run.archive_id == item.archive_id
        and run.queue_item_id in (None, item.id)
    )

    stop_sent = stop_print_by_user(printer_id, mark_when_unsent=not is_the_bound_run, unless_idle=False)
    if not stop_sent:
        logger.warning("stop_print returned False for printer %s - printer may not be connected", printer_id)

    if is_the_bound_run and not stop_sent:
        # ⚠️ Nothing was stopped, so nothing is closed and nothing is marked. The print may well be running
        # on a printer we merely cannot reach: closing the row and the archive here left its real finish
        # refused (no ``printing`` row to accept), its filament unbooked, the archive "cancelled by user"
        # for a print that completed, and the run bound until the operator cleared it. Without the mark a
        # later real failure stays a failure and a real finish is a completed print. The print is closed by
        # the printer's own report, or by reconciliation after a reconnect or restart. Only the operator's
        # pause is taken (``is_paused`` -- never ``status``, which still says the printer is printing), so
        # nothing is dispatched behind this print.
        queue = await db.get(PrinterQueue, item.queue_id, populate_existing=True)
        if queue is not None:
            queue.is_paused = True
        await db.commit()
        logger.warning(
            "Stop for printing queue item %s (archive %s) could not be sent; the print stays running, queue paused",
            item_id,
            item.archive_id,
        )
        raise HTTPException(
            503,
            detail={
                "error": "stop_not_sent",
                "message": (
                    "The stop was not sent: the printer is not reachable. The print is still considered running "
                    "and will be closed by the printer's own report or after it reconnects. The queue is paused."
                ),
            },
        )

    if is_the_bound_run:
        # ⚠️ The printer's own report ends this print, not this route. The completion handler accepts a bound
        # run's terminal only while its queue row is still ``printing`` -- it closes the row, the archive and
        # the queue in one transaction and books the filament. Cancelling the row here, a few seconds ahead
        # of that report, left the handler with no row to accept: the archive stayed ``printing`` for good,
        # the run stayed bound in memory and the printer took no further job until a restart (0.7.3). The
        # user-stopped mark above makes that report read as ``cancelled``, and the handler pauses the queue.
        # A printer that never reports the stop keeps the row ``printing`` -- which is then the truth, and
        # Print state diagnostics closes it.
        logger.info(
            "Stop sent for printing queue item %s (archive %s); the printer's report closes it",
            item_id,
            item.archive_id,
        )
        if auto_off_after:
            from backend.app.services.smart_plug_manager import smart_plug_manager

            try:
                await smart_plug_manager.schedule_off_after_queue_job(printer_id, db)
            except Exception as e:
                logger.warning("Auto-off: Failed to schedule power-off for printer %s: %s", printer_id, e)
        return {"message": "Print stopped"}

    # No bound run to report it (the job was still being sent or heated -- nothing on the printer owns
    # it): the row is closed here, whether or not the stop could be sent -- but only the attempt this Stop
    # read, in a fresh session under its lane (G03 B, A16): the archive's guard first, then the lane's.
    await db.rollback()  # the request's read ends before the lane writer (A16)
    outcome = await _stop_unbound_attempt(
        item_id=item_id,
        queue_id=printer_id,
        identity=read_identity,
        token=read_token,
        archive_id=read_archive_id,
        stop_sent=stop_sent,
    )
    if outcome == "bound":
        # A run became bound to this row's archive meanwhile: the printer's report closes it.
        return {"message": "Print stopped"}
    if outcome == "already_stopped":
        # The dispatcher honoured this Stop's cancel signal, or the operator cancelled the job meanwhile.
        return {"message": "Print stopped"}
    if outcome == "changed":
        raise HTTPException(
            409,
            detail={
                "error": "stop_target_changed",
                "message": (
                    "This job is no longer the one that was being sent: it was sent again, moved or finished before "
                    "the stop was recorded. Nothing was changed; check the queue."
                ),
            },
        )

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

    # Clear manual_start flag so scheduler picks it up — a head-defining column, so under the row's lane (G03 B,
    # A7/A15): one compare-and-set on the row, still pending, in the queue it was read in.
    #
    # Attribute the print to the operator who started it when the item has no
    # owner yet (upstream #1670). An item added through the UI already carries
    # its uploader in created_by_id — preserve that; only fill the gap for
    # otherwise-unattributed items so the dispatched archive (our print log)
    # isn't left with an empty User column when auth is on.
    from backend.app.core import database
    from backend.app.services.queue_ops import queue_claim_scope

    queue = item.queue_id
    values: dict = {"manual_start": False}
    if user is not None:
        values["created_by_id"] = func.coalesce(PrintQueueItem.created_by_id, user.id)
    await db.rollback()  # the read ends before the lane
    async with database.async_session() as wdb, queue_claim_scope(wdb, queue):
        # Only in the queue it was read in, whose lane is held (L1): a row moved since is never locked.
        await wdb.scalar(
            select(PrintQueueItem.id)
            .where(PrintQueueItem.id == item_id, PrintQueueItem.queue_id == queue)
            .with_for_update(key_share=True)
        )
        result = await wdb.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == item_id, PrintQueueItem.status == "pending", PrintQueueItem.queue_id == queue)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            current = await wdb.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
            if current is None:
                raise HTTPException(404, "Queue item not found")
            if current == "pending":  # moved to another queue meanwhile
                raise HTTPException(
                    409,
                    detail={
                        "error": "queue_item_changed",
                        "message": "This job changed or started while it was being edited. Nothing was saved; refresh and try again.",
                    },
                )
            raise HTTPException(400, f"Can only start pending items, current status: '{current}'")
        await wdb.commit()

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
        .execution_options(populate_existing=True)
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


_QUEUE_READ_ALL = require_permission(Permission.QUEUE_READ_ALL)


async def _ensure_may_clone(
    db: AsyncSession,
    creds: RequestCredentials,
    user: User | None,
    rows: list[PrintQueueItem],
    keep_order: bool,
) -> None:
    """A clone is new work made from someone's rows (WS-13 E13 Q-02): the rows must be the
    caller's own or readable with ``queue:read_all`` (404 otherwise — like a missing id), and an
    inherited order asks the Workshop's filing right and an open order. ``keep_order=false`` is
    the explicit choice to clone without it."""
    if not await creds.allows(_QUEUE_READ_ALL) and any(user is None or row.created_by_id != user.id for row in rows):
        raise HTTPException(404, "Queue item not found")
    links = {(row.project_id, row.project_line_id) for row in rows} - {(None, None)}
    if keep_order and links:
        from backend.app.services.order_filing import resolve_link

        await ensure_may_file_future(creds)
        for project_id, project_line_id in sorted(links, key=str):
            await resolve_link(db, project_id, project_line_id)


@router.post("/{item_id}/clone", response_model=PrintQueueItemResponse)
async def clone_item_endpoint(
    item_id: int,
    scope: str = Query("single", pattern="^(single|batch)$"),
    keep_order: bool = Query(True, description="Clone under the source's order (the default) or without it"),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
    creds: RequestCredentials = Depends(request_credentials),
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
    from backend.app.services.queue_ops import clone_batch, clone_item, get_batch_pending_items

    src = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not src:
        raise HTTPException(404, "Queue item not found")
    # The batch of THIS row's queue (G03 D1): the copies AutoQueue gave other printers are not cloned here.
    rows = (
        await get_batch_pending_items(db, src.batch_id, queue_id=src.queue_id)
        if scope == "batch" and src.batch_id
        else [src]
    )
    await _ensure_may_clone(db, creds, current_user, rows or [src], keep_order)
    as_new = {
        "created_by_id": current_user.id if current_user else None,
        "keep_order": keep_order,
        "authority": await creds.capture(),
    }

    try:
        if scope == "batch":
            if not src.batch_id:
                raise HTTPException(400, "Item is not part of a batch")
            clones = await clone_batch(db, src.batch_id, queue_id=src.queue_id, **as_new)
            if not clones:
                raise HTTPException(400, "No pending items in batch to clone")
            await update_queue_counters(db, clones[0].queue_id)
            await db.commit()
            first = clones[0]
        else:
            first = await clone_item(db, item_id, keep_batch=True, **as_new)
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
    """Set a pending item's status to ``skipped`` — scheduler won't pick it.

    A compare-and-set on ``pending`` (G03 E-2): a row the scheduler claimed after the read above is
    left printing, and the answer says the row moved on instead of reporting a skip that did not happen.
    """
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import set_status

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Only pending items can be skipped, current status: '{item.status}'")

    queue_id = item.queue_id
    from backend.app.services.queue_ops import lane_session

    await db.rollback()  # the read ends before the lane (A7)
    async with lane_session([queue_id]) as wdb:
        if not await set_status(wdb, item_id, "skipped", commit=False, queue_id=queue_id):
            raise HTTPException(
                409,
                detail={
                    "error": "recovery_changed",
                    "message": "This job changed before it could be skipped. It may have started already; nothing was skipped.",
                },
            )
        await update_queue_counters(wdb, queue_id)
        await wdb.commit()
    return {"status": "skipped", "item_id": item_id}


async def _acknowledge_blocking_failure(db: AsyncSession, queue_id: int) -> None:
    """Retire the failure that the ``require_previous_success`` gate is reading.

    The gate has ONE reader, ``services/previous_print_gate``: the same lookback and order
    (``completed_at DESC NULLS LAST, id DESC``, G03 E3a) as the gate itself. It marks the newest
    finished row only when that row is the failure — if a print has succeeded since, nothing is
    gating and there is nothing to acknowledge. Never commits.
    """
    from backend.app.services.previous_print_gate import acknowledge_newest_failure

    await acknowledge_newest_failure(db, queue_id)


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

    from backend.app.services import queue_source_link
    from backend.app.services.filament_policy import clear_routing_runtime

    # As Retry (G03 B, A27): the storage guard and the lane for every job, a legacy one included; the candidate's
    # parent before the job row; the row re-read under them — the copy read before the lane is never written.
    read_queue = item.queue_id
    link_plan = queue_source_link.plan_link(item)
    unchanged = {
        "error": "recovery_changed",
        "message": "This job changed before it could be put back in the queue. Refresh the queue; nothing was changed.",
    }
    try:
        async with reusing_sources(db, [item.queue_source_id], queue_ids=[read_queue]):
            await queue_source_link.lock_plans(db, [link_plan])
            # Only in the queue it was read in, whose lane is held (L1).
            item = await db.scalar(
                select(PrintQueueItem)
                .where(PrintQueueItem.id == item_id, PrintQueueItem.queue_id == read_queue)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
            if item is None or item.status != "skipped" or item.queue_id != read_queue:
                raise HTTPException(409, detail=unchanged)
            if not queue_source_link.apply_link(item, link_plan):
                raise HTTPException(409, detail=unchanged)
            if item.require_previous_success:
                await _acknowledge_blocking_failure(db, item.queue_id)

            max_pos = (
                await db.execute(
                    select(func.max(PrintQueueItem.position))
                    .where(PrintQueueItem.queue_id == item.queue_id)
                    .where(PrintQueueItem.status == "pending")
                )
            ).scalar() or 0
            clear_routing_runtime(item)
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

    If the item is part of a batch, toggle is propagated to the pending
    siblings in ITS queue so the batch behaves consistently there -- the
    copies AutoQueue gave other printers keep their own flag (G03 D1).
    """

    item = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Only pending items can be toggled, current status: '{item.status}'")

    new_value = not item.manual_start
    queue, batch, incarnation = item.queue_id, item.batch_id, item.recovery_identity
    from backend.app.services.queue_ops import lane_session, lock_named_rows

    await db.rollback()  # the read ends before the lane (A7)
    changed = HTTPException(
        409,
        detail={
            "error": "queue_item_changed",
            "message": "This job changed or started while it was being edited. Nothing was saved; refresh and try again.",
        },
    )
    async with lane_session([queue]) as wdb:
        # The clicked row as it was read (A7): the same incarnation, still pending in this queue, in the same
        # group, the value not flipped yet. Group membership changes only under this lane, so a group that changed
        # since the read is refused whole -- its former members are never touched.
        row = (await lock_named_rows(wdb, {item_id: queue})).get(item_id)
        if (
            row is None
            or row.status != "pending"
            or row.recovery_identity != incarnation
            or row.batch_id != batch
            or row.manual_start == new_value
        ):
            raise changed
        target = PrintQueueItem.batch_id == batch if batch else PrintQueueItem.id == item_id
        # The batch's pending siblings in ITS queue follow (G03 D1), compare-and-set on pending + the lane.
        await wdb.execute(
            update(PrintQueueItem)
            .where(target, PrintQueueItem.queue_id == queue, PrintQueueItem.status == "pending")
            .values(manual_start=new_value)
            .execution_options(synchronize_session=False)
        )
        # The answer is the clicked row's value, read back in the transaction that wrote it.
        if await wdb.scalar(select(PrintQueueItem.manual_start).where(PrintQueueItem.id == item_id)) != new_value:
            await wdb.rollback()
            raise changed
        await wdb.commit()
    return {"manual_start": new_value, "item_id": item_id}


@router.post("/bulk-retry")
async def retry_failed_block(
    request: Request,
    data: PrintQueueBulkRetry,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Retry confirmed failed/cancelled jobs whose reviewed status still matches."""
    from backend.app.models.api_key import APIKey
    from backend.app.services.queue_bulk_retry import retry_block
    from backend.app.services.swap_source_declarations import actor_for

    actor = await actor_for(request, db, user)
    key = await db.get(APIKey, actor.key_id) if actor.key_id else None
    result = await retry_block(
        db,
        data.queue_id,
        data.item_ids,
        actor=actor,
        granted_scope=key_printer_scope(request),
        key_hash=key.key_hash if key else None,
        cancelled_item_ids=data.cancelled_item_ids,
    )
    _end_capture_cooldowns(result["retried"])
    return result


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

    from backend.app.services import queue_source_link
    from backend.app.services.filament_policy import clear_routing_runtime

    # The source link this Retry may write, from the row as read before the guards (G03 B, A27).
    link_plan = queue_source_link.plan_link(item)
    read_queue = item.queue_id
    try:
        async with reusing_sources(db, [item.queue_source_id], queue_ids=[read_queue]):
            from backend.app.services.print_execution import require_repeatable

            # The candidate's parent before the job row (L5, L6(d)); the row only in the queue it was read in,
            # whose lane is held (L1).
            await queue_source_link.lock_plans(db, [link_plan])
            item = await db.scalar(
                select(PrintQueueItem)
                .where(PrintQueueItem.id == item_id, PrintQueueItem.queue_id == read_queue)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
            if item is None or item.status not in ("failed", "cancelled"):
                raise HTTPException(
                    409,
                    detail={
                        "error": "recovery_changed",
                        "message": "This job changed before it could be retried. Refresh the queue; nothing was retried.",
                    },
                )
            await require_repeatable(db, item)
            from backend.app.services.plate_hold import plate_gate_owner_ids

            # The plate gate waits on this very row: only the plate answer may re-arm it
            # (Repeat print releases the gate in the same transaction). Re-armed here, the
            # gate would keep pointing at a row that is no longer finished and refuse every
            # answer while the queue could not start (spec plate-gate-owner-requeue A1).
            if await plate_gate_owner_ids(db, [item.id]):
                raise HTTPException(
                    409,
                    detail={
                        "error": "plate_answer_required",
                        "message": "The printer is waiting for a plate answer about this print. "
                        "Answer on the printer card: Repeat print runs it again",
                    },
                )
            max_pos = (
                await db.execute(
                    select(func.max(PrintQueueItem.position))
                    .where(PrintQueueItem.queue_id == item.queue_id)
                    .where(PrintQueueItem.status == "pending")
                )
            ).scalar() or 0
            if not queue_source_link.apply_link(item, link_plan):
                raise HTTPException(
                    409,
                    detail={
                        "error": "recovery_changed",
                        "message": "This job changed before it could be retried. Refresh the queue; nothing was retried.",
                    },
                )
            clear_routing_runtime(item)
            item.status = "pending"
            item.position = max_pos + 1
            item.error_message = None
            item.error_code = None
            item.completed_at = None
            # The operator's Retry is a new intent, so a new bounded budget (owner,
            # 2026-10-23); automatic recovery never resets it. ``started_at`` stays:
            # it is the proof this row was dispatched before (``swap_job_refresh``).
            # A halted queue stays halted — Resume is the operator's own step.
            item.dispatch_attempts = 0
            await update_queue_counters(db, item.queue_id)
            await db.commit()
        _end_capture_cooldowns([item.id])
        from backend.app.services import dispatch_stall_tracker

        dispatch_stall_tracker.new_incarnation(item.id)
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
#
# ⚠️ A batch action touches ONE printer queue (G03 D1, owner decision 2026-11-19). AutoQueue promotion
# copies the router row's ``batch_id`` onto every printer row it creates, so the copies of one AutoQueue
# batch sit in the queues of the printers that took them. Every ``/batch/{batch_id}/…`` route takes the
# queue the operator is looking at as ``?queue_id=``; without it the batch's only queue is used, and a
# batch whose active rows span several is refused (409 ``batch_spans_queues``) -- an old client must
# not cancel or move other printers' work by accident. The archive keeps the shared ``batch_id``, so
# history still groups every copy.

_BATCH_QUEUE = Query(
    None,
    gt=0,
    description="The printer queue whose rows of the batch to act on; required when the batch spans several",
)


async def _batch_queue_id(db: AsyncSession, batch_id: str, queue_id: int | None) -> int | None:
    """The queue a batch route acts on, or the 409 that says the caller must name one."""
    from backend.app.services.queue_ops import BatchSpansQueues, batch_queue_id

    try:
        return await batch_queue_id(db, batch_id, queue_id)
    except BatchSpansQueues as exc:
        raise HTTPException(
            409,
            detail={
                "error": "batch_spans_queues",
                "message": "This batch has jobs in several printer queues. Choose the printer queue to act on.",
            },
        ) from exc


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
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import lane_session, lock_named_rows

    queue = next(iter(queue_ids))
    ids = sorted(set(data.item_ids))
    await db.rollback()  # the read ends before the lane (A7)
    async with lane_session([queue]) as wdb:
        await lock_named_rows(wdb, dict.fromkeys(ids, queue))
        # All or nothing: every named row still pending in this queue, or none is grouped.
        result = await wdb.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id.in_(ids), PrintQueueItem.status == "pending", PrintQueueItem.queue_id == queue)
            .values(batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != len(ids):
            await wdb.rollback()
            raise HTTPException(
                409,
                detail={
                    "error": "queue_item_changed",
                    "message": "This job changed or started while it was being edited. Nothing was saved; refresh and try again.",
                },
            )
        await update_queue_counters(wdb, queue)
        await wdb.commit()
    logger.info("Grouped %s queue items into batch %s", len(ids), batch_id)
    return {"batch_id": batch_id, "count": len(ids)}


@router.post("/batch/{batch_id}/ungroup")
async def ungroup_batch(
    batch_id: str,
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Disband a batch (upstream #1743): clear batch_id from its still-in-queue
    members (pending + printing) of one printer queue (G03 D1). Completed/cancelled
    history keeps its grouping, and so do the copies other printers hold."""
    from backend.app.services.queue_ops import ACTIVE_BATCH_STATUSES

    target = await _batch_queue_id(db, batch_id, queue_id)
    items = []
    if target is not None:
        result = await db.execute(
            select(PrintQueueItem).where(
                PrintQueueItem.batch_id == batch_id,
                PrintQueueItem.queue_id == target,
                PrintQueueItem.status.in_(ACTIVE_BATCH_STATUSES),
            )
        )
        items = result.scalars().all()
    if not items:
        raise HTTPException(404, "No active items in this batch")
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import lane_session

    await db.rollback()  # the read ends before the lane (A7)
    async with lane_session([target]) as wdb:
        # Pending and printing members (cosmetic on a printing one: its execution archive copied the value its
        # runner read), Core, under the lane.
        result = await wdb.execute(
            update(PrintQueueItem)
            .where(
                PrintQueueItem.batch_id == batch_id,
                PrintQueueItem.queue_id == target,
                PrintQueueItem.status.in_(ACTIVE_BATCH_STATUSES),
            )
            .values(batch_id=None)
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            raise HTTPException(404, "No active items in this batch")
        await update_queue_counters(wdb, target)
        await wdb.commit()
    logger.info("Ungrouped batch %s in queue %s (%s items)", batch_id, target, result.rowcount)
    return {"ungrouped": result.rowcount, "batch_id": batch_id, "queue_id": target}


@router.post("/batch/{batch_id}/cancel")
async def cancel_batch(
    batch_id: str,
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Cancel the batch's pending items in one printer queue (G03 D1).  Active (printing) item is unaffected.

    The rows' lanes are taken before the authoritative read (``cancellation_scope``), the write is a
    compare-and-set on ``status='pending'`` -- a copy the scheduler claimed meanwhile is left printing --
    and each cancelled row is dated, which the "previous print succeeded" gate orders by (G03 E3a).
    """
    from backend.app.core import database
    from backend.app.services.queue_cancel_scope import cancellation_scope, selection_changed
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import set_status_for_batch

    def _rows_of(target):
        return select(PrintQueueItem.id).where(PrintQueueItem.batch_id == batch_id, PrintQueueItem.queue_id == target)

    async with database.async_session() as preflight:
        target = await _batch_queue_id(preflight, batch_id, queue_id)
        ids = [] if target is None else list((await preflight.scalars(_rows_of(target))).all())
    if target is None:
        return {"cancelled": 0}
    async with cancellation_scope(ids) as db:
        current_ids = set((await db.scalars(_rows_of(target))).all())
        if not current_ids <= set(ids) or await _batch_queue_id(db, batch_id, queue_id) != target:
            raise selection_changed()
        count = await set_status_for_batch(
            db, batch_id, "cancelled", queue_id=target, completed_at=datetime.now(timezone.utc), commit=False
        )
        if not count:
            return {"cancelled": 0}
        await update_queue_counters(db, target)
        await db.commit()
        return {"cancelled": count, "batch_id": batch_id, "queue_id": target}


@router.post("/batch/{batch_id}/skip")
async def skip_batch(
    batch_id: str,
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Skip the batch's pending items in one printer queue (G03 D1), compare-and-set on ``pending``."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import lane_session, set_status_for_batch

    target = await _batch_queue_id(db, batch_id, queue_id)
    if target is None:
        return {"skipped": 0}
    await db.rollback()  # the read ends before the lane (A7)
    async with lane_session([target]) as wdb:
        count = await set_status_for_batch(wdb, batch_id, "skipped", queue_id=target, commit=False)
        if not count:
            return {"skipped": 0}
        await update_queue_counters(wdb, target)
        await wdb.commit()
    return {"skipped": count, "batch_id": batch_id, "queue_id": target}


async def _pending_block(db: AsyncSession, batch_id: str, queue_id: int | None) -> tuple[int, list[int]]:
    """The batch's pending rows of the one queue a block move acts on -- 404 when it has none there."""
    from backend.app.services.queue_ops import get_batch_pending_items

    target = await _batch_queue_id(db, batch_id, queue_id)
    pending = [] if target is None else await get_batch_pending_items(db, batch_id, queue_id=target)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    return target, [i.id for i in pending]


@router.post("/batch/{batch_id}/reorder")
async def reorder_batch(
    batch_id: str,
    direction: str = Query(..., pattern="^(up|down)$"),
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a whole batch block one step up/down in one printer queue (G03 D1)."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import reorder_block

    target, block_ids = await _pending_block(db, batch_id, queue_id)
    moved = await reorder_block(db, target, block_ids, direction)
    if moved:
        await update_queue_counters(db, target)
        await db.commit()
    return {"moved": moved, "direction": direction, "batch_size": len(block_ids), "queue_id": target}


@router.post("/batch/{batch_id}/bump")
async def bump_batch(
    batch_id: str,
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a whole batch to the top of one printer queue (G03 D1)."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_top

    target, block_ids = await _pending_block(db, batch_id, queue_id)
    shifted = await bump_block_to_top(db, target, block_ids)
    if shifted:
        await update_queue_counters(db, target)
        await db.commit()
    return {"shifted": shifted, "batch_size": len(block_ids), "queue_id": target}


@router.post("/batch/{batch_id}/bump-bottom")
async def bump_batch_bottom(
    batch_id: str,
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Move a whole batch to the bottom of one printer queue (G03 D1)."""
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import bump_block_to_bottom

    target, block_ids = await _pending_block(db, batch_id, queue_id)
    shifted = await bump_block_to_bottom(db, target, block_ids)
    if shifted:
        await update_queue_counters(db, target)
        await db.commit()
    return {"shifted": shifted, "batch_size": len(block_ids), "queue_id": target}


@router.patch("/batch/{batch_id}")
async def update_batch(
    batch_id: str,
    data: PrintQueueItemUpdate,
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.QUEUE_UPDATE_ALL),
):
    """Apply a partial update to the batch's pending items in one printer queue (G03 D1).

    ``?queue_id=`` names the queue whose copies change; the body's own ``queue_id`` moves them to
    another queue. Both queues' counters are recounted.
    """
    from backend.app.services.queue_ops import get_batch_pending_items

    target = await _batch_queue_id(db, batch_id, queue_id)
    pending = [] if target is None else await get_batch_pending_items(db, batch_id, queue_id=target)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    bases = {item.id: row_basis(item) for item in pending}  # what the batch edit is validated against (A6)

    update_data = data.model_dump(exclude_unset=True)
    if "ams_mapping" in update_data:
        update_data["ams_mapping"] = json.dumps(update_data["ams_mapping"]) if update_data["ams_mapping"] else None
    # Serialize H2C rack-swap nozzle pick (#1780); mirrors ams_mapping above.
    if "nozzle_mapping" in update_data:
        update_data["nozzle_mapping"] = (
            json.dumps(update_data["nozzle_mapping"]) if update_data["nozzle_mapping"] else None
        )
    if "nozzle_rack_choice" in update_data:
        update_data["nozzle_rack_choice"] = (
            json.dumps(update_data["nozzle_rack_choice"]) if update_data["nozzle_rack_choice"] else None
        )
    if "selected_macro_ids" in update_data:
        ids = update_data["selected_macro_ids"]
        update_data["selected_macro_ids"] = json.dumps(ids) if ids is not None else None

    # A move is checked as the single PATCH checks it (G03 follow-up): the target must exist, every
    # member's own bytes must fit the target printer -- or the queue it stays in, for a new plate -- and
    # the target's swap rule decides the swap columns. All of it before the first write: the batch is
    # refused whole, or written whole (swap-source-lifecycle spec r4 §8).
    # The queue the copies land in is read once, before the checks (A6, A25): they reach the rows that read
    # refreshed, and the plans carry what they judged.
    moving_to = update_data.get("queue_id")
    if moving_to == target:
        moving_to = None
    facts = await checked_facts(db, [moving_to if moving_to is not None else target])
    batch_queue, batch_model = None, None
    if moving_to is not None:
        if facts[moving_to] is None:
            raise HTTPException(400, "Queue not found")
        batch_queue = facts.queue(moving_to)
        if batch_queue.printer_id is not None:
            batch_model = facts[moving_to][3]
    elif facts[target] is None:
        raise _queue_edit_conflict()
    moves = batch_queue is not None and batch_queue.id != target
    if "plate_id" in update_data or moves:
        check_queue = batch_queue if moves else facts.queue(target)
        if check_queue is not None and check_queue.printer_id is not None:
            check_model = batch_model if moves else facts[target][3]
            cache = PrintRequirementsCache()
            for item in pending:
                await _check_item_model_for_queue(
                    db, item, check_queue, update_data.get("plate_id", item.plate_id), cache, check_model
                )

    swap_updates = {item.id: await _swap_update(db, item, update_data, batch_queue, facts) for item in pending}
    update_data.pop("swap_macro_events", None)

    plans = []
    for item in pending:
        item_update = await routing_update(db, item, update_data, snapshot=facts)
        item_update.update(swap_updates[item.id])
        _drop_rack_pick_on_move(item, item_update, batch_queue, batch_model)
        if isinstance(item_update.get("ams_mapping"), list):
            item_update["ams_mapping"] = json.dumps(item_update["ams_mapping"])
        plans.append(
            plan_edit(
                item,
                item_update,
                basis=bases[item.id],
                target_queue_id=batch_queue.id if batch_queue is not None else None,
                facts=facts,
            )
        )
    # The batch moves whole or not at all (A6): applied under its lanes, refused when any copy changed.
    await db.rollback()
    outcome = await apply_edits(plans, set_field=_set_item_field, all_or_nothing=True)
    if outcome.changed:
        raise _queue_edit_conflict()
    _end_capture_cooldowns(outcome.applied)
    return {
        "updated": len(outcome.applied),
        "batch_id": batch_id,
        "fields": list(update_data.keys()),
        "queue_id": target,
    }


@router.post("/batch/{batch_id}/clone")
async def clone_batch_endpoint(
    batch_id: str,
    scope: str = Query("batch", pattern="^(one|batch)$"),
    keep_order: bool = Query(True, description="Clone under the source's order (the default) or without it"),
    queue_id: int | None = _BATCH_QUEUE,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Clone a batch's copies in one printer queue (G03 D1).

    ``scope='one'`` — add one more copy to the same batch (appended).
    ``scope='batch'`` — create a whole new batch with the same
    configuration as the source.

    Every member of the batch is still asked ``require_repeatable``, wherever it
    sits: a stale claim among the other printers' copies refuses the clone as before.

    ⚠️ m173: the SECOND door onto ``clone_item`` / ``clone_batch``, and therefore
    the second that has to answer the capture taxonomy. Both services refuse a
    blob that is not ``ready``, and an unmapped refusal here leaves as a bare 500
    — on the operator's screen the difference between "the file is unreadable" and
    "BamDude is broken", and the frontend branches on ``detail.code``.
    """
    from backend.app.services.print_execution import require_repeatable
    from backend.app.services.queue_counters import update_queue_counters
    from backend.app.services.queue_ops import clone_batch, clone_item, get_batch_pending_items

    batch_members = list((await db.scalars(select(PrintQueueItem).where(PrintQueueItem.batch_id == batch_id))).all())
    try:
        for member in batch_members:
            await require_repeatable(db, member, whole_batch=True)
    except QueueSourceError as exc:
        raise refusal(exc) from exc
    target = await _batch_queue_id(db, batch_id, queue_id)
    pending = [] if target is None else await get_batch_pending_items(db, batch_id, queue_id=target)
    if not pending:
        raise HTTPException(404, "No pending items in batch")
    await _ensure_may_clone(db, creds, current_user, pending[:1] if scope == "one" else pending, keep_order)
    as_new = {
        "created_by_id": current_user.id if current_user else None,
        "keep_order": keep_order,
        "authority": await creds.capture(),
    }

    try:
        if scope == "one":
            new_item = await clone_item(db, pending[0].id, keep_batch=True, **as_new)
            if new_item is None:
                raise HTTPException(500, "Clone failed")
            await update_queue_counters(db, target)
            await db.commit()
            return {"cloned": 1, "scope": "one", "batch_id": batch_id, "new_item_id": new_item.id, "queue_id": target}

        clones = await clone_batch(db, batch_id, queue_id=target, **as_new)
        if not clones:
            raise HTTPException(500, "Clone failed")
        await update_queue_counters(db, target)
        await db.commit()
    except QueueSourceError as exc:
        raise refusal(exc) from exc
    return {
        "cloned": len(clones),
        "scope": "batch",
        "source_batch_id": batch_id,
        "new_batch_id": clones[0].batch_id,
        "queue_id": target,
    }
