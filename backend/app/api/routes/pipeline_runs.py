"""API routes for Slicer Pipeline runs (#1425, ported to BamDude).

One "Run pipeline" click = slice the source once with the pipeline's presets,
then enqueue ``copies`` print jobs against the pipeline's target using its
fanout strategy. Each copy is a ``PipelineJob``; the run rolls the per-copy
statuses up into a single run status.

**BamDude two-tier queue adaptation.** Upstream enqueues a flat
``PrintQueueItem(printer_id=…, target_model=…)`` per copy. BamDude's
``PrintQueueItem`` has no ``target_model`` column and ``printer_id`` is a derived
property, so the fanout splits (see ``_enqueue_copies``):

  * **Pinned printer** (specific-printer target, or a class target resolved to a
    concrete printer via ``fill_one_first`` / ``round_robin``) → a
    ``PrintQueueItem`` appended to that printer's ``PrinterQueue``. Tracked via
    ``PipelineJob.queue_entry_id`` + ``assigned_printer_id``.
  * **Model class** (``max_parallel``, or no matching printer) → an
    ``AutoQueueItem(target_model=…)`` the AutoQueue distributor assigns
    dynamically, spawning a ``print_queue`` row (``source_auto_item_id`` links
    back). Tracked via ``PipelineJob.auto_queue_item_id``.

The slice itself runs through ``slice_dispatch`` (same path as the manual
SliceModal) so the "Slicing X — 75%" progress toast renders end-to-end; the
slice job's id rides on the run response so the frontend can call ``trackJob``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.config import settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.pipeline_run import PipelineJob, PipelineRun
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.slicer_pipeline import SlicerPipeline
from backend.app.models.user import User
from backend.app.schemas.pipeline_run import (
    CheckEligibilityRequest,
    EligibilityIssueResponse,
    EligibilityReportResponse,
    PerPrinterReport as PerPrinterReportResponse,
    PipelineJobResponse,
    PipelineRunCreateRequest,
    PipelineRunListResponse,
    PipelineRunResponse,
)
from backend.app.schemas.slicer import PresetRef, SliceRequest
from backend.app.services.pipeline_eligibility import (
    EligibilityReport,
    check_pipeline_eligibility,
)

logger = logging.getLogger(__name__)


pipeline_run_create_router = APIRouter(prefix="/slicer-pipelines", tags=["Slicer Pipelines"])
pipeline_run_router = APIRouter(prefix="/pipeline-runs", tags=["Slicer Pipelines"])

_TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
_TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "partial_failure")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_status(report: EligibilityReport) -> EligibilityReportResponse:
    return EligibilityReportResponse(
        ok=report.ok,
        target_kind=report.target_kind,
        target_printer_id=report.target_printer_id,
        target_printer_name=report.target_printer_name,
        target_model_class=report.target_model_class,
        issues=[
            EligibilityIssueResponse(
                kind=issue.kind,
                slot_index=issue.slot_index,
                expected=issue.expected,
                actual=issue.actual,
            )
            for issue in report.issues
        ],
        printer_reports=[
            PerPrinterReportResponse(
                printer_id=r.printer_id,
                printer_name=r.printer_name,
                ok=r.ok,
                issues=[
                    EligibilityIssueResponse(
                        kind=i.kind,
                        slot_index=i.slot_index,
                        expected=i.expected,
                        actual=i.actual,
                    )
                    for i in r.issues
                ],
            )
            for r in report.printer_reports
        ],
    )


async def _load_pipeline(db: AsyncSession, pipeline_id: int) -> SlicerPipeline:
    pipeline = (
        await db.execute(
            select(SlicerPipeline).where(
                SlicerPipeline.id == pipeline_id,
                SlicerPipeline.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(404, "Pipeline not found")
    return pipeline


async def _load_printer_status(printer_id: int | None) -> dict | None:
    """Snapshot the printer_manager's live PrinterState for the eligibility
    matcher. Returns ``None`` when the printer has no MQTT client."""
    if printer_id is None:
        return None
    from backend.app.services.printer_manager import printer_manager

    state = printer_manager.get_status(printer_id)
    if state is None:
        return None
    return {"connected": state.connected, "raw_data": state.raw_data}


def _make_status_lookup():
    """Closure that snapshots the printer_manager once per printer_id call.
    Passed to the matcher's class-targeting branch so it can read live state
    for every candidate printer."""

    def _lookup(printer_id: int) -> dict | None:
        from backend.app.services.printer_manager import printer_manager

        state = printer_manager.get_status(printer_id)
        if state is None:
            return None
        return {"connected": state.connected, "raw_data": state.raw_data}

    return _lookup


def _slice_request_from_pipeline(pipeline: SlicerPipeline) -> SliceRequest:
    try:
        raw_filaments = json.loads(pipeline.filament_presets_json or "[]")
    except (json.JSONDecodeError, TypeError):
        raw_filaments = []
    filament_presets = [
        PresetRef(source=r["source"], id=r["id"])
        for r in raw_filaments
        if isinstance(r, dict) and "source" in r and "id" in r
    ]
    return SliceRequest(
        printer_preset=PresetRef(source=pipeline.printer_preset_source, id=pipeline.printer_preset_id),
        process_preset=PresetRef(source=pipeline.process_preset_source, id=pipeline.process_preset_id),
        filament_presets=filament_presets,
        bed_type=pipeline.bed_type,
        export_3mf=True,
    )


def _compute_job_status(persisted: str, queue_entry: PrintQueueItem | None) -> str:
    """Map a persisted job status + its live print_queue row onto the job's
    live status. Terminal-persisted wins; else follow the queue entry."""
    if persisted in _TERMINAL_JOB_STATUSES:
        return persisted
    if queue_entry is None:
        return persisted
    qs = queue_entry.status
    if qs == "completed":
        return "completed"
    if qs in ("failed", "aborted"):
        return "failed"
    if qs == "cancelled":
        return "cancelled"
    if qs == "printing":
        return "printing"
    return "queued"


async def _printer_id_for_queue_entry(session: AsyncSession, queue_entry: PrintQueueItem | None) -> int | None:
    if queue_entry is None:
        return None
    return (
        await session.execute(select(PrinterQueue.printer_id).where(PrinterQueue.id == queue_entry.queue_id))
    ).scalar_one_or_none()


async def _spawned_queue_entry_for_auto(
    session: AsyncSession, auto_item: AutoQueueItem | None
) -> PrintQueueItem | None:
    """The print_queue row the AutoQueue distributor spawned for this auto item
    (``source_auto_item_id`` links it back, ``assigned_to_item_id`` on the auto
    row is the forward pointer)."""
    if auto_item is None:
        return None
    if auto_item.assigned_to_item_id is not None:
        qe = (
            await session.execute(select(PrintQueueItem).where(PrintQueueItem.id == auto_item.assigned_to_item_id))
        ).scalar_one_or_none()
        if qe is not None:
            return qe
    return (
        await session.execute(select(PrintQueueItem).where(PrintQueueItem.source_auto_item_id == auto_item.id))
    ).scalar_one_or_none()


async def _resolve_job_state(session: AsyncSession, job: PipelineJob) -> tuple[str, int | None, int | None]:
    """Return ``(live_status, assigned_printer_id, effective_queue_entry_id)``.

    Dual-path (BamDude): a pinned copy tracks a ``print_queue`` row via
    ``queue_entry_id``; a model-class copy tracks an ``auto_queue_item_id`` that
    the AutoQueue distributor later assigns, spawning a ``print_queue`` row.
    """
    # Pinned path.
    if job.queue_entry_id is not None:
        qe = (
            await session.execute(select(PrintQueueItem).where(PrintQueueItem.id == job.queue_entry_id))
        ).scalar_one_or_none()
        live = _compute_job_status(job.status, qe)
        # Dispatched then the queue entry was deleted from the queue page →
        # operator intent was cancellation (else the run sits forever queued).
        if qe is None and live not in _TERMINAL_JOB_STATUSES:
            live = "cancelled"
        printer_id = job.assigned_printer_id or await _printer_id_for_queue_entry(session, qe)
        return live, printer_id, job.queue_entry_id

    # Model-class path (AutoQueue staging row).
    if job.auto_queue_item_id is not None:
        auto = (
            await session.execute(select(AutoQueueItem).where(AutoQueueItem.id == job.auto_queue_item_id))
        ).scalar_one_or_none()
        if job.status in _TERMINAL_JOB_STATUSES:
            return job.status, job.assigned_printer_id, None
        if auto is None:
            return "cancelled", job.assigned_printer_id, None
        if auto.status == "cancelled":
            return "cancelled", job.assigned_printer_id, None
        if auto.status == "pending":
            return "awaiting_printer", None, None
        # assigned → follow the spawned print_queue row for the real status.
        qe = await _spawned_queue_entry_for_auto(session, auto)
        live = _compute_job_status(job.status, qe)
        if qe is None and live not in _TERMINAL_JOB_STATUSES:
            live = "cancelled"
        printer_id = (await _printer_id_for_queue_entry(session, qe)) or job.assigned_printer_id
        return live, printer_id, (qe.id if qe else None)

    # Neither downstream row yet (pre-dispatch pending job).
    return job.status, job.assigned_printer_id, job.queue_entry_id


def _roll_up_run_status(persisted: str, job_statuses: list[str]) -> str:
    """Compute the run-level status from the per-job statuses.

    Terminal-persisted cancels always win so the dashboard doesn't flicker when
    one job's queue entry hasn't caught up. Otherwise:
      - all completed → completed
      - any in-flight → in_progress (unless still queued/slicing/dispatching)
      - all cancelled → cancelled
      - some completed + some failed/cancelled → partial_failure
      - any failed → failed
    """
    if persisted in ("cancelled",):
        return persisted
    if not job_statuses:
        return persisted

    completed = sum(1 for s in job_statuses if s == "completed")
    failed = sum(1 for s in job_statuses if s == "failed")
    cancelled = sum(1 for s in job_statuses if s == "cancelled")
    in_flight = sum(1 for s in job_statuses if s in ("printing", "queued", "awaiting_printer", "pending"))
    total = len(job_statuses)

    if completed == total:
        return "completed"
    if in_flight > 0:
        return "in_progress" if persisted not in ("queued", "slicing", "dispatching") else persisted
    # All copies are in terminal states.
    if failed == 0 and cancelled == total:
        return "cancelled"
    if completed > 0 and (failed > 0 or cancelled > 0):
        return "partial_failure"
    if failed > 0:
        return "failed"
    return persisted


async def _materialise_run(db: AsyncSession, run: PipelineRun) -> PipelineRunResponse:
    pipeline_name: str | None = None
    target_kind = None
    target_printer_id = None
    target_model_class = None
    fanout_strategy = None
    if run.pipeline_id:
        pipeline = (
            await db.execute(select(SlicerPipeline).where(SlicerPipeline.id == run.pipeline_id))
        ).scalar_one_or_none()
        if pipeline:
            pipeline_name = pipeline.name
            target_kind = pipeline.target_kind  # type: ignore[assignment]
            target_printer_id = pipeline.target_printer_id
            target_model_class = pipeline.target_model_class
            fanout_strategy = pipeline.fanout_strategy  # type: ignore[assignment]

    source_filename: str | None = None
    if run.source_library_file_id:
        src = (
            await db.execute(select(LibraryFile).where(LibraryFile.id == run.source_library_file_id))
        ).scalar_one_or_none()
        source_filename = src.filename if src else None
    elif run.source_archive_id:
        arc = (
            await db.execute(select(PrintArchive).where(PrintArchive.id == run.source_archive_id))
        ).scalar_one_or_none()
        source_filename = (arc.print_name or arc.filename) if arc else None

    job_rows = (
        (
            await db.execute(
                select(PipelineJob).where(PipelineJob.pipeline_run_id == run.id).order_by(PipelineJob.copy_index)
            )
        )
        .scalars()
        .all()
    )

    job_responses: list[PipelineJobResponse] = []
    job_live_statuses: list[str] = []
    for job in job_rows:
        live_job_status, printer_id, effective_qe_id = await _resolve_job_state(db, job)

        printer_name: str | None = None
        if printer_id:
            p = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
            printer_name = p.name if p else None

        job_live_statuses.append(live_job_status)
        job_responses.append(
            PipelineJobResponse(
                id=job.id,
                pipeline_run_id=job.pipeline_run_id,
                copy_index=job.copy_index,
                assigned_printer_id=printer_id,
                assigned_printer_name=printer_name,
                queue_entry_id=effective_qe_id,
                status=live_job_status,  # type: ignore[arg-type]
                error_message=job.error_message,
                dispatched_at=job.dispatched_at,
                completed_at=job.completed_at,
            )
        )

    rolled_up = _roll_up_run_status(run.status, job_live_statuses)

    return PipelineRunResponse(
        id=run.id,
        pipeline_id=run.pipeline_id,
        pipeline_name=pipeline_name,
        source_library_file_id=run.source_library_file_id,
        source_archive_id=run.source_archive_id,
        source_filename=source_filename,
        parent_run_id=run.parent_run_id,
        copies=run.copies,
        copies_completed=sum(1 for s in job_live_statuses if s == "completed"),
        copies_failed=sum(1 for s in job_live_statuses if s == "failed"),
        copies_cancelled=sum(1 for s in job_live_statuses if s == "cancelled"),
        copies_in_progress=sum(
            1 for s in job_live_statuses if s in ("printing", "queued", "awaiting_printer", "pending")
        ),
        status=rolled_up,  # type: ignore[arg-type]
        slice_job_id=run.slice_job_id,
        sliced_library_file_id=run.sliced_library_file_id,
        eligibility_overridden=run.eligibility_overridden,
        error_message=run.error_message,
        created_by=run.created_by,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        jobs=job_responses,
        target_kind=target_kind,
        target_printer_id=target_printer_id,
        target_model_class=target_model_class,
        fanout_strategy=fanout_strategy,
    )


async def _publish_run_event(db: AsyncSession, run: PipelineRun) -> None:
    """Broadcast a ``pipeline_run_updated`` event with the full materialised
    run so the dashboard refreshes live."""
    try:
        payload = await _materialise_run(db, run)
        await ws_manager.send_pipeline_run_updated(payload.model_dump(mode="json"))
    except Exception:
        logger.exception("Failed to broadcast pipeline_run_updated for run %d", run.id)


# ---------------------------------------------------------------------------
# Source resolution + fanout + orchestration
# ---------------------------------------------------------------------------


SourceKind = Literal["library_file", "archive"]


async def _resolve_source(
    db: AsyncSession,
    *,
    library_file_id: int | None,
    archive_id: int | None,
) -> tuple[SourceKind, int, str, Path]:
    if library_file_id is not None:
        lib = (await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))).scalar_one_or_none()
        if lib is None:
            raise HTTPException(404, "Source library file not found")
        src_path = (
            Path(app_settings.base_dir) / lib.file_path
        )  # SEC-PATH-OK: lib.file_path is a LibraryFile DB column set only by the upload route.
        if not src_path.exists():
            raise HTTPException(404, "Source library file missing on disk")
        return ("library_file", lib.id, lib.filename, src_path)

    assert archive_id is not None
    arc = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if arc is None:
        raise HTTPException(404, "Source archive not found")
    rel = arc.source_3mf_path or arc.file_path
    if not rel:
        raise HTTPException(400, "Archive has no source file to slice")
    src_path = (
        Path(app_settings.base_dir) / rel
    )  # SEC-PATH-OK: rel is archive.source_3mf_path / archive.file_path, set by upload-time validators.
    if not src_path.exists():
        raise HTTPException(404, "Archive source file missing on disk")
    name = arc.filename or arc.print_name or src_path.name
    return ("archive", arc.id, name, src_path)


async def _pick_assignments(
    db: AsyncSession,
    pipeline: SlicerPipeline,
    copies: int,
) -> list[tuple[int | None, str | None]]:
    """Return ``[(printer_id_or_None, target_model_or_None), ...]`` of length
    ``copies`` per the pipeline's fanout strategy.

    ``printer_id`` set → pinned copy (goes to that printer's PrinterQueue).
    ``target_model`` set (printer_id None) → model-class copy (goes to the
    AutoQueue, distributor picks any free matching printer). ``_enqueue_copies``
    consumes this."""
    target_kind = pipeline.target_kind or "specific_printer"
    if target_kind == "specific_printer" or pipeline.target_printer_id is not None:
        assert pipeline.target_printer_id is not None
        return [(pipeline.target_printer_id, None)] * copies

    # Class-targeting. Enumerate matching active printers + apply the strategy.
    matching = (
        (
            await db.execute(
                select(Printer)
                .where(Printer.model == pipeline.target_model_class)
                .where(Printer.is_active.is_(True))
                .where(Printer.archived.is_(False))
                .order_by(Printer.id)
            )
        )
        .scalars()
        .all()
    )
    if not matching:
        # Eligibility normally blocks this, but fail gracefully: stage every
        # copy on the AutoQueue and let the distributor route them if a
        # matching printer later comes online.
        return [(None, pipeline.target_model_class)] * copies

    strategy = pipeline.fanout_strategy or "max_parallel"
    if strategy == "fill_one_first":
        # Pin every copy to the first match (serial dispatch to one printer).
        return [(matching[0].id, None)] * copies
    if strategy == "round_robin":
        # Cycle through eligible printers — copy ``i`` → ``matching[i % n]``.
        return [(matching[i % len(matching)].id, None) for i in range(copies)]
    # max_parallel — leave printer_id=None + set target_model so the AutoQueue
    # distributor balances copies across any free matching printer.
    return [(None, pipeline.target_model_class)] * copies


async def _enqueue_copies(
    session: AsyncSession,
    *,
    jobs: list[PipelineJob],
    assignments: list[tuple[int | None, str | None]],
    sliced_library_file_id: int,
    creator_user_id: int | None,
) -> None:
    """Create the downstream queue rows for each copy (BamDude two-tier adaptation).

    Pinned copy (``printer_id`` set) → a ``PrintQueueItem`` appended to that
    printer's ``PrinterQueue`` (same insertion shape as ``add_to_queue`` /
    ``auto_queue_scheduler`` — find queue by ``printer_id``, ``MAX(position)+1``,
    ``library_file_id``, ``status='pending'``). Tracked via ``job.queue_entry_id``.

    Model-class copy (``target_model`` set, ``printer_id`` None) → an
    ``AutoQueueItem`` (same shape as ``add_to_auto_queue``) that the AutoQueue
    distributor assigns dynamically. Tracked via ``job.auto_queue_item_id``.
    """
    now = datetime.now(timezone.utc)
    for job, (printer_id, target_model) in zip(jobs, assignments, strict=False):
        if printer_id is not None:
            printer_queue = (
                await session.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
            ).scalar_one_or_none()
            if printer_queue is None:
                # Every printer has exactly one PrinterQueue (created at registration,
                # unique on printer_id). A missing one is a data-integrity anomaly —
                # surface it exactly as auto_queue_scheduler does, rather than papering
                # over it with a stray queue. The orchestration's handler marks the run
                # failed. Do NOT self-heal our queue ideology here.
                raise RuntimeError(f"Printer {printer_id} has no PrinterQueue row")
            max_pos = await session.scalar(
                select(func.coalesce(func.max(PrintQueueItem.position), 0)).where(
                    PrintQueueItem.queue_id == printer_queue.id
                )
            )
            queue_item = PrintQueueItem(
                queue_id=printer_queue.id,
                library_file_id=sliced_library_file_id,
                position=(max_pos or 0) + 1,
                status="pending",
                created_by_id=creator_user_id,
            )
            session.add(queue_item)
            await session.flush()
            job.queue_entry_id = queue_item.id
            job.assigned_printer_id = printer_id
        else:
            max_pos = await session.scalar(
                select(func.coalesce(func.max(AutoQueueItem.position), 0)).where(AutoQueueItem.status == "pending")
            )
            auto_item = AutoQueueItem(
                library_file_id=sliced_library_file_id,
                target_model=target_model,
                position=(max_pos or 0) + 1,
                status="pending",
                created_by_id=creator_user_id,
            )
            session.add(auto_item)
            await session.flush()
            job.auto_queue_item_id = auto_item.id
            job.assigned_printer_id = None
        # Set unconditionally — the orchestration actually reached the enqueue.
        job.dispatched_at = now


async def _cancel_job_downstream(session: AsyncSession, job: PipelineJob) -> None:
    """Cancel whichever downstream row a job created — a pinned print_queue
    entry, or an AutoQueue staging row (+ any print_queue row it spawned) — as
    long as the row is still cancellable (``pending``/``queued`` / not started).
    In-flight prints on the printer keep going (operator must Stop)."""
    if job.queue_entry_id is not None:
        qe = (
            await session.execute(select(PrintQueueItem).where(PrintQueueItem.id == job.queue_entry_id))
        ).scalar_one_or_none()
        if qe is not None and qe.status in ("pending", "queued"):
            qe.status = "cancelled"
    if job.auto_queue_item_id is not None:
        auto = (
            await session.execute(select(AutoQueueItem).where(AutoQueueItem.id == job.auto_queue_item_id))
        ).scalar_one_or_none()
        if auto is not None:
            spawned = await _spawned_queue_entry_for_auto(session, auto)
            if spawned is not None and spawned.status in ("pending", "queued"):
                spawned.status = "cancelled"
            if auto.status in ("pending", "assigned"):
                auto.status = "cancelled"
                auto.cancelled_at = datetime.now(timezone.utc)


def _make_orchestration_callable(
    *,
    run_id: int,
    pipeline_id: int,
    src_kind: SourceKind,
    src_id: int,
    src_filename: str,
    src_path: Path,
    creator_user_id: int | None,
    copies: int,
):
    """Returns the async callable ``slice_dispatch.enqueue`` runs as the
    background slice job. Wraps slice + multi-copy enqueue + state update."""

    async def _orchestrate(slice_job_id: int) -> dict:
        from backend.app.api.routes.library import slice_and_persist
        from backend.app.core.database import async_session

        async with async_session() as session:
            run = (await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
            pipeline = (
                await session.execute(select(SlicerPipeline).where(SlicerPipeline.id == pipeline_id))
            ).scalar_one_or_none()
            if run is None or pipeline is None:
                logger.warning("pipeline_run %d or pipeline %d disappeared mid-orchestration", run_id, pipeline_id)
                return {}

            # Honour a cancel that landed between ``POST /run`` returning and
            # this background task starting. If the run was cancelled while
            # still ``queued`` we must NOT flip it back to ``slicing``.
            if run.status == "cancelled":
                logger.info("pipeline_run %d was cancelled before slicing started", run_id)
                return {}

            run.status = "slicing"
            run.started_at = datetime.now(timezone.utc)
            await session.commit()
            await _publish_run_event(session, run)

            slice_request = _slice_request_from_pipeline(pipeline)
            model_bytes = src_path.read_bytes()

            folder_id: int | None = None
            if src_kind == "library_file":
                lib = (await session.execute(select(LibraryFile).where(LibraryFile.id == src_id))).scalar_one_or_none()
                if lib is not None:
                    folder_id = lib.folder_id

            try:
                slice_response = await slice_and_persist(
                    session,
                    model_bytes=model_bytes,
                    model_filename=src_filename,
                    folder_id=folder_id,
                    extra_metadata={
                        f"sliced_from_{src_kind}_id": src_id,
                        "sliced_via_pipeline_id": pipeline.id,
                        "sliced_via_pipeline_run_id": run.id,
                    },
                    request=slice_request,
                    current_user_id=creator_user_id,
                    job_id=slice_job_id,
                )
            except HTTPException as exc:
                run.status = "failed"
                run.error_message = f"Slice failed: {exc.detail}"
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await _publish_run_event(session, run)
                raise
            except Exception as exc:
                logger.exception("Pipeline run %d slice raised unexpectedly", run_id)
                run.status = "failed"
                run.error_message = f"Slice failed: {exc}"
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await _publish_run_event(session, run)
                raise

            run.sliced_library_file_id = slice_response.library_file_id

            # Re-check cancellation: slicing can take minutes and the operator
            # may have hit Cancel. Refresh from the DB rather than trusting our
            # in-memory ``run`` (the cancel route writes via a separate session).
            await session.refresh(run)
            if run.status == "cancelled":
                logger.info("pipeline_run %d cancelled mid-slice; skipping queue enqueue", run_id)
                await session.commit()
                return slice_response.model_dump()

            # Enqueue N copies per the picked assignment strategy.
            assignments = await _pick_assignments(session, pipeline, copies)

            jobs = (
                (
                    await session.execute(
                        select(PipelineJob)
                        .where(PipelineJob.pipeline_run_id == run_id)
                        .order_by(PipelineJob.copy_index)
                    )
                )
                .scalars()
                .all()
            )
            if len(jobs) != copies:
                logger.warning("pipeline_run %d expected %d jobs, found %d", run_id, copies, len(jobs))

            await _enqueue_copies(
                session,
                jobs=jobs,
                assignments=assignments,
                sliced_library_file_id=slice_response.library_file_id,
                creator_user_id=creator_user_id,
            )

            # Final cancellation check before committing 'dispatching'. The
            # cancel route writes via a separate session so we refresh to see
            # the latest. If the cancel landed in this narrow window — AFTER the
            # post-slice refresh but BEFORE this commit — cancel the downstream
            # rows we just created so the user's intent sticks.
            await session.refresh(run)
            if run.status == "cancelled":
                logger.info(
                    "pipeline_run %d cancelled in the dispatch window; cancelling its %d downstream rows",
                    run_id,
                    len(jobs),
                )
                for job in jobs:
                    await _cancel_job_downstream(session, job)
                    if job.status not in _TERMINAL_JOB_STATUSES:
                        job.status = "cancelled"
                        job.completed_at = datetime.now(timezone.utc)
                await session.commit()
                await _publish_run_event(session, run)
                return slice_response.model_dump()

            for job in jobs:
                job.status = "queued"
            run.status = "dispatching"
            await session.commit()
            await _publish_run_event(session, run)

            return slice_response.model_dump()

    return _orchestrate


# ---------------------------------------------------------------------------
# /slicer-pipelines/{id}/check-eligibility
# ---------------------------------------------------------------------------


@pipeline_run_create_router.post("/{pipeline_id}/check-eligibility", response_model=EligibilityReportResponse)
async def check_eligibility(
    pipeline_id: int,
    body: CheckEligibilityRequest,
    _: User | None = RequirePermission(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    pipeline = await _load_pipeline(db, pipeline_id)
    await _resolve_source(
        db,
        library_file_id=body.source_library_file_id,
        archive_id=body.source_archive_id,
    )
    if pipeline.target_kind == "printer_class" and pipeline.target_printer_id is None:
        report = await check_pipeline_eligibility(db, pipeline, status_lookup=_make_status_lookup())
    else:
        status = await _load_printer_status(pipeline.target_printer_id)
        report = await check_pipeline_eligibility(db, pipeline, status)
    return _serialise_status(report)


# ---------------------------------------------------------------------------
# /slicer-pipelines/{id}/run
# ---------------------------------------------------------------------------


@pipeline_run_create_router.post("/{pipeline_id}/run", response_model=PipelineRunResponse, status_code=202)
async def run_pipeline(
    pipeline_id: int,
    body: PipelineRunCreateRequest,
    current_user: User | None = RequirePermission(Permission.PIPELINES_RUN),
    db: AsyncSession = Depends(get_db),
):
    from backend.app.api.routes.settings import get_setting
    from backend.app.services.slice_dispatch import slice_dispatch

    pipeline = await _load_pipeline(db, pipeline_id)
    src_kind, src_id, src_filename, src_path = await _resolve_source(
        db,
        library_file_id=body.source_library_file_id,
        archive_id=body.source_archive_id,
    )

    # Cap copies against the configured ceiling.
    raw_cap = await get_setting(db, "pipeline_max_copies")
    try:
        cap = int(raw_cap) if raw_cap else 50
    except (TypeError, ValueError):
        cap = 50
    if body.copies > cap:
        raise HTTPException(
            422,
            f"copies={body.copies} exceeds pipeline_max_copies setting ({cap})",
        )

    # Eligibility pre-flight.
    if pipeline.target_kind == "printer_class" and pipeline.target_printer_id is None:
        report = await check_pipeline_eligibility(db, pipeline, status_lookup=_make_status_lookup())
    else:
        status = await _load_printer_status(pipeline.target_printer_id)
        report = await check_pipeline_eligibility(db, pipeline, status)

    if not report.ok and not body.force:
        raise HTTPException(status_code=409, detail=_serialise_status(report).model_dump())

    # Need a target — specific or class — to dispatch.
    if pipeline.target_printer_id is None and not pipeline.target_model_class:
        raise HTTPException(
            400,
            "Pipeline has no target. Open the pipeline in Settings → Workflow → Pipelines "
            "and choose a target printer or printer class.",
        )

    run = PipelineRun(
        pipeline_id=pipeline.id,
        source_library_file_id=src_id if src_kind == "library_file" else None,
        source_archive_id=src_id if src_kind == "archive" else None,
        copies=body.copies,
        status="queued",
        eligibility_overridden=(not report.ok and body.force),
        created_by=current_user.id if current_user else None,
    )
    db.add(run)
    await db.flush()

    # One PipelineJob per copy.
    for i in range(body.copies):
        db.add(
            PipelineJob(
                pipeline_run_id=run.id,
                copy_index=i,
                status="pending",
            )
        )
    await db.commit()
    await db.refresh(run)
    await _publish_run_event(db, run)

    orchestrate = _make_orchestration_callable(
        run_id=run.id,
        pipeline_id=pipeline.id,
        src_kind=src_kind,
        src_id=src_id,
        src_filename=src_filename,
        src_path=src_path,
        creator_user_id=current_user.id if current_user else None,
        copies=body.copies,
    )
    slice_job = await slice_dispatch.enqueue(
        kind="library_file" if src_kind == "library_file" else "archive",
        source_id=src_id,
        source_name=src_filename,
        run=orchestrate,
    )

    run.slice_job_id = slice_job.id
    await db.commit()
    await db.refresh(run)

    return await _materialise_run(db, run)


# ---------------------------------------------------------------------------
# Lists, reads, cancel, retry-failed
# ---------------------------------------------------------------------------


@pipeline_run_create_router.get("/{pipeline_id}/runs", response_model=PipelineRunListResponse)
async def list_runs_for_pipeline(
    pipeline_id: int,
    limit: int = 10,
    _: User | None = RequirePermission(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    limit = max(1, min(limit, 100))
    rows = (
        (
            await db.execute(
                select(PipelineRun)
                .where(PipelineRun.pipeline_id == pipeline_id)
                .order_by(PipelineRun.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = (
        await db.execute(select(func.count()).select_from(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id))
    ).scalar() or 0
    return PipelineRunListResponse(
        runs=[await _materialise_run(db, r) for r in rows],
        total=total,
    )


@pipeline_run_router.get("", response_model=PipelineRunListResponse)
async def list_all_runs(
    limit: int = 25,
    offset: int = 0,
    pipeline_id: int | None = None,
    status: str | None = None,
    target_printer_id: int | None = None,
    target_model_class: str | None = None,
    _: User | None = RequirePermission(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    """Dashboard list. Newest first; filters on pipeline_id + status +
    target_printer_id + target_model_class. The ``status`` filter matches the
    persisted snapshot, not the live roll-up. ``target_*`` filters JOIN to the
    pipeline so runs whose pipeline currently points at the printer / class are
    returned."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    stmt = select(PipelineRun)
    count_stmt = select(func.count()).select_from(PipelineRun)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineRun.pipeline_id == pipeline_id)
        count_stmt = count_stmt.where(PipelineRun.pipeline_id == pipeline_id)
    if status:
        stmt = stmt.where(PipelineRun.status == status)
        count_stmt = count_stmt.where(PipelineRun.status == status)
    if target_printer_id is not None or target_model_class is not None:
        stmt = stmt.join(SlicerPipeline, SlicerPipeline.id == PipelineRun.pipeline_id)
        count_stmt = count_stmt.join(SlicerPipeline, SlicerPipeline.id == PipelineRun.pipeline_id)
        if target_printer_id is not None:
            stmt = stmt.where(SlicerPipeline.target_printer_id == target_printer_id)
            count_stmt = count_stmt.where(SlicerPipeline.target_printer_id == target_printer_id)
        if target_model_class is not None:
            stmt = stmt.where(SlicerPipeline.target_model_class == target_model_class)
            count_stmt = count_stmt.where(SlicerPipeline.target_model_class == target_model_class)

    rows = (await db.execute(stmt.order_by(desc(PipelineRun.id)).offset(offset).limit(limit))).scalars().all()
    total = (await db.execute(count_stmt)).scalar() or 0

    return PipelineRunListResponse(
        runs=[await _materialise_run(db, r) for r in rows],
        total=total,
    )


@pipeline_run_router.post("/clear")
async def clear_terminal_runs(
    _: User | None = RequirePermission(Permission.PIPELINES_WRITE),
    db: AsyncSession = Depends(get_db),
):
    """Delete every terminal pipeline run (completed / failed / cancelled /
    partial_failure). In-flight runs are preserved. Cascades to PipelineJob;
    the linked PrintQueueItem / AutoQueueItem rows keep their own lifecycle."""
    count_stmt = select(func.count()).select_from(PipelineRun).where(PipelineRun.status.in_(_TERMINAL_RUN_STATUSES))
    n = (await db.execute(count_stmt)).scalar() or 0
    if n > 0:
        await db.execute(delete(PipelineRun).where(PipelineRun.status.in_(_TERMINAL_RUN_STATUSES)))
        await db.commit()
    return {"deleted": n}


@pipeline_run_router.get("/{run_id}", response_model=PipelineRunResponse)
async def get_run(
    run_id: int,
    _: User | None = RequirePermission(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Pipeline run not found")
    return await _materialise_run(db, run)


@pipeline_run_router.post("/{run_id}/cancel", response_model=PipelineRunResponse)
async def cancel_run(
    run_id: int,
    _: User | None = RequirePermission(Permission.PIPELINES_RUN),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a queued / in-flight run. Cascades to all cancellable downstream
    queue rows; in-flight prints continue on the printer (operator must Stop)."""
    run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Pipeline run not found")

    if run.status in _TERMINAL_RUN_STATUSES:
        return await _materialise_run(db, run)

    run.status = "cancelled"
    run.completed_at = datetime.now(timezone.utc)
    if not run.error_message:
        run.error_message = "Cancelled by user"

    job_rows = (await db.execute(select(PipelineJob).where(PipelineJob.pipeline_run_id == run.id))).scalars().all()
    for job in job_rows:
        await _cancel_job_downstream(db, job)
        if job.status not in _TERMINAL_JOB_STATUSES:
            job.status = "cancelled"
            job.completed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(run)
    await _publish_run_event(db, run)
    return await _materialise_run(db, run)


@pipeline_run_router.post("/{run_id}/retry-failed", response_model=PipelineRunResponse, status_code=202)
async def retry_failed(
    run_id: int,
    current_user: User | None = RequirePermission(Permission.PIPELINES_RUN),
    db: AsyncSession = Depends(get_db),
):
    """Create a new run with copies = (failed + cancelled count) from the
    parent. Same pipeline, same source. Eligibility re-checked at run time (the
    operator may have fixed the issue since)."""
    parent = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if parent is None:
        raise HTTPException(404, "Pipeline run not found")
    if parent.pipeline_id is None:
        raise HTTPException(400, "Original pipeline was deleted; cannot retry")
    if parent.source_library_file_id is None and parent.source_archive_id is None:
        raise HTTPException(400, "Original source was deleted; cannot retry")

    # Count the parent's failed + cancelled jobs by their live status.
    parent_jobs = (
        (await db.execute(select(PipelineJob).where(PipelineJob.pipeline_run_id == parent.id))).scalars().all()
    )
    fail_count = 0
    for j in parent_jobs:
        live, _printer_id, _qe_id = await _resolve_job_state(db, j)
        if live in ("failed", "cancelled"):
            fail_count += 1

    if fail_count == 0:
        raise HTTPException(400, "No failed copies to retry")

    body = PipelineRunCreateRequest(
        source_library_file_id=parent.source_library_file_id,
        source_archive_id=parent.source_archive_id,
        copies=fail_count,
        force=True,  # operator already accepted eligibility on the parent
    )

    # Reuse the run_pipeline route logic via a direct call — single-sources the
    # orchestration. The result inherits parent_run_id.
    new_run_response = await run_pipeline(parent.pipeline_id, body, current_user=current_user, db=db)

    new_row = (await db.execute(select(PipelineRun).where(PipelineRun.id == new_run_response.id))).scalar_one_or_none()
    if new_row is not None:
        new_row.parent_run_id = parent.id
        await db.commit()
        await db.refresh(new_row)
        return await _materialise_run(db, new_row)
    return new_run_response
