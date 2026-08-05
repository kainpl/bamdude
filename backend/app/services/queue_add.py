"""Creating a print-queue item — one definition, two callers.

The single-item route (``POST /print-queue/``) and the file manager's bulk add
both need every gate performed here: the queue must exist and be unpaused, the
caller must own what they are queueing, the filename must survive FAT32, and a
G-code 3MF sliced for one printer model must not be queued to a printer that
cannot run it (#2578).

A bulk path that re-implemented any of it would drift from this one, and the
drift would surface as prints that fail at dispatch rather than as an error
somebody sees. The bulk caller converts these ``HTTPException``s into per-item
errors; that is why they stay exceptions rather than becoming a result type —
otherwise the single route would have to translate them back.
"""

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.models.user import User
from backend.app.schemas.calibration_mode import mode_to_bool
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services.library_helpers import project_for_library_file
from backend.app.utils.filename import InvalidFilenameError, is_sliced_file, validate_print_filename
from backend.app.utils.printer_models import is_gcode_compatible


async def add_items_to_printer_queue(
    db: AsyncSession,
    data: PrintQueueItemCreate,
    current_user: User | None,
) -> tuple[list[PrintQueueItem], PrinterQueue]:
    """Validate, build and persist ``data.quantity`` queue items.

    Returns the created items (in position order) and the queue they landed in,
    so the caller can build its own response without re-querying for the
    printer's name.
    """
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

    # A paused queue refuses new items — pause means "queue closed".
    if queue.is_paused:
        raise HTTPException(409, "Queue is paused — resume it before adding items")

    # Validate archive exists (if provided) and get it for filament extraction
    archive = None
    if data.archive_id:
        result = await db.execute(select(PrintArchive).where(PrintArchive.id == data.archive_id))
        archive = result.scalar_one_or_none()
        if not archive:
            raise HTTPException(400, "Archive not found")
        # IDOR fix (security #2): a caller with QUEUE_CREATE could otherwise
        # queue any user's archive without read access to it. Gate on
        # ARCHIVES_READ_ALL or ownership; 404 (not 403) so we don't leak
        # "this id exists but you can't queue it".
        if (
            current_user is not None
            and not current_user.has_permission(Permission.ARCHIVES_READ_ALL.value)
            and archive.created_by_id != current_user.id
        ):
            raise HTTPException(404, "Archive not found")

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
        # Same IDOR gate for cross-user library-file queueing (security #2).
        if (
            current_user is not None
            and not current_user.has_permission(Permission.LIBRARY_READ_ALL.value)
            and library_file.created_by_id != current_user.id
        ):
            raise HTTPException(404, "Library file not found")

        # Pre-flight: refuse a FAT32-illegal filename at queue time rather than
        # letting the item sit pending only to fail at FTP dispatch (upstream #1540).
        try:
            validate_print_filename(library_file.filename)
        except InvalidFilenameError as e:
            raise HTTPException(400, str(e))

        # Same reason, one step earlier: an STL has nothing to send. The
        # dispatcher refuses it too, but by then the item has been sitting
        # pending and the refusal reaches nobody. Held by the bulk library route
        # until that route was replaced by the Schedule dialog; it belongs here,
        # where every caller passes.
        if not is_sliced_file(library_file.filename):
            raise HTTPException(400, "Not a sliced file. Only .gcode or .gcode.3mf files can be printed.")

    # Cross-model safety gate (#2578): a G-code 3MF sliced for one model must not
    # be queued to a printer it can't run on. This is the per-printer tier — the
    # item binds to this queue's printer and the dispatcher hands it straight over
    # with no human in the loop — so an API-created (or UI) mismatch is rejected
    # here. Missing slice metadata never blocks (see is_gcode_compatible).
    sliced_for = None
    if archive:
        sliced_for = archive.sliced_for_model
    elif library_file and library_file.file_metadata:
        sliced_for = library_file.file_metadata.get("sliced_for_model")
    if sliced_for and queue.printer_id is not None:
        from backend.app.models.printer import Printer

        printer_model = (
            await db.execute(select(Printer.model).where(Printer.id == queue.printer_id))
        ).scalar_one_or_none()
        if not is_gcode_compatible(sliced_for, printer_model):
            raise HTTPException(
                400,
                f"File was sliced for {sliced_for} and cannot be dispatched to a {printer_model} printer",
            )

    # Serialize concurrent inserts into the same queue so two appends can't both
    # read the same MAX(position) and land on a duplicate position in an empty
    # scope (upstream #1625-followup TOCTOU fix). A transaction-scoped Postgres
    # advisory lock keyed on the queue closes the window and releases at
    # commit/rollback; different queues don't contend. SQLite serialises writes
    # implicitly so this is a no-op there. The dialect is read from the live
    # session binding (not settings/is_sqlite()) because a test fixture can bind
    # SQLite while settings.database_url still points at Postgres.
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        scope_key = data.queue_id if data.queue_id is not None else 0
        # classid 1625 namespaces the lock so it can't collide with other
        # advisory locks elsewhere in the codebase.
        await db.execute(text("SELECT pg_advisory_xact_lock(1625, :k)"), {"k": scope_key})

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

    # One rule, shared with the auto-queue and direct-print routes — it used to
    # be written out here and there, and the third place never got a copy.
    effective_project_id = project_for_library_file(data.project_id, library_file)

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
                require_previous_success=data.require_previous_success,
                ams_mapping=ams_mapping_json,
                plate_id=data.plate_id,
                bed_levelling=mode_to_bool(data.bed_levelling),
                bed_levelling_mode=data.bed_levelling,
                flow_cali=mode_to_bool(data.flow_cali),
                flow_cali_mode=data.flow_cali,
                layer_inspect=data.layer_inspect,
                timelapse=data.timelapse,
                use_ams=data.use_ams,
                nozzle_offset_cali=mode_to_bool(data.nozzle_offset_cali),
                nozzle_offset_cali_mode=data.nozzle_offset_cali,
                mesh_mode_fast_check=data.mesh_mode_fast_check,
                execute_swap_macros=execute_swap_macros,
                swap_macro_events=swap_macro_events_json,
                gcode_injection=data.gcode_injection,
                preheat_override=data.preheat_override,
                preheat_chamber_target_override=data.preheat_chamber_target_override,
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

    # Update queue counters (full recount for accuracy)
    from backend.app.services.queue_counters import update_queue_counters

    await update_queue_counters(db, data.queue_id)
    await db.commit()

    return items, queue
