"""Background dispatch for print/reprint jobs.

This service is separate from the app's print queue feature. It exists only to
decouple "send/start print" operations (FTP upload + start command) from API
request latency so the UI can continue immediately after dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.services.archive import ArchiveService
from backend.app.services.bambu_ftp import (
    delete_file_async,
    get_ftp_retry_settings,
    list_files_async,
    upload_file_async,
    with_ftp_retry,
)
from backend.app.services.gcode_patcher import GcodeInjectionSpec
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)


class DispatchJobCancelled(Exception):
    """Raised when a dispatch job is cancelled by the user."""


class DispatchEnqueueRejected(Exception):
    """Raised when a dispatch job should not be accepted."""


@dataclass(slots=True)
class PrintDispatchJob:
    id: int
    kind: Literal["reprint_archive", "print_library_file"]
    source_id: int
    source_name: str
    printer_id: int
    printer_name: str
    options: dict[str, Any] = field(default_factory=dict)
    requested_by_user_id: int | None = None
    requested_by_username: str | None = None
    project_id: int | None = None
    cleanup_library_after_dispatch: bool = False
    # Link back to a ``print_queue.id`` when the dispatch was requested by
    # the scheduler for a queue item.  The runner updates the queue item's
    # ``archive_id`` once the archive row is created so the two FSMs stay
    # in sync without a second DB round-trip from the scheduler.
    queue_item_id: int | None = None
    # Signalled at the very end of ``_run_*`` (success / failure / cancel)
    # so ``run_from_queue_item`` callers can await the outcome.
    completion_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Populated by the runner before it sets ``completion_event``.  Shape:
    # ``{"success": bool, "archive_id": int | None, "error": str | None, "cancelled": bool}``.
    outcome: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActiveDispatchState:
    job: PrintDispatchJob
    message: str
    upload_bytes: int | None = None
    upload_total_bytes: int | None = None


class BackgroundDispatchService:
    def __init__(self):
        self._queued_jobs: deque[PrintDispatchJob] = deque()
        self._dispatcher_task: asyncio.Task | None = None
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        # Serializes only the DB-write *startup* phase of each job
        # (archive INSERT + queue_item linking + commit). Once that phase
        # ends, the lock releases and FTP / start_print / post-write phases
        # of multiple jobs run in parallel. Replaces the older "one job at
        # a time across all printers" gate that contended on the SQLite
        # write lock when ``archive_print``'s INSERT raced an open FTP
        # session's still-uncommitted txn.
        self._startup_lock = asyncio.Lock()
        self._job_event = asyncio.Event()
        self._next_job_id = 1
        self._active_jobs: dict[int, ActiveDispatchState] = {}
        self._cancel_requested_job_ids: set[int] = set()

        # Progress for the current "batch" (since queue became non-empty)
        self._batch_total = 0
        self._batch_completed = 0
        self._batch_failed = 0

    @staticmethod
    def _printer_is_busy_printing(printer_id: int) -> bool:
        state = printer_manager.get_status(printer_id)
        if not state:
            return False
        return state.state in ("RUNNING", "PAUSE", "PAUSED") and bool(state.gcode_file)

    async def start(self):
        async with self._lock:
            if self._dispatcher_task and not self._dispatcher_task.done():
                return
            self._dispatcher_task = asyncio.create_task(self._dispatcher_loop(), name="background-dispatch-dispatcher")
            logger.info("Background dispatch dispatcher started")

    async def stop(self):
        dispatcher: asyncio.Task | None = None
        running_tasks: list[asyncio.Task] = []
        async with self._lock:
            dispatcher = self._dispatcher_task
            self._dispatcher_task = None
            running_tasks = list(self._running_tasks.values())
            self._running_tasks.clear()
            self._active_jobs.clear()
            self._queued_jobs.clear()
            self._cancel_requested_job_ids.clear()
            self._job_event.set()

        if dispatcher:
            dispatcher.cancel()
        for task in running_tasks:
            task.cancel()

        if dispatcher:
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass

        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

        logger.info("Background dispatch dispatcher stopped")

    async def dispatch_reprint_archive(
        self,
        *,
        archive_id: int,
        archive_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
    ) -> dict[str, Any]:
        return await self._dispatch(
            kind="reprint_archive",
            source_id=archive_id,
            source_name=archive_name,
            printer_id=printer_id,
            printer_name=printer_name,
            options=options,
            requested_by_user_id=requested_by_user_id,
            requested_by_username=requested_by_username,
        )

    async def get_state(self) -> dict[str, Any]:
        """Get current dispatch queue state snapshot for newly connected clients."""
        async with self._lock:
            return self._build_state_payload_unlocked()

    async def run_from_queue_item(
        self,
        *,
        kind: Literal["reprint_archive", "print_library_file"],
        source_id: int,
        source_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        queue_item_id: int,
    ) -> dict[str, Any]:
        """Run a dispatch inline (bypass queue) on behalf of the scheduler.

        The scheduler already gates on stagger + printer-idle, so we don't
        need to re-enqueue through the BackgroundDispatch queue here — we
        run the job directly, still registering it as "active" so the UI
        shows it while the FTP upload and print-start happen. Returns the
        job's ``outcome`` dict once ``_run_*`` signals completion.
        """
        async with self._lock:
            job = PrintDispatchJob(
                id=self._next_job_id,
                kind=kind,
                source_id=source_id,
                source_name=source_name,
                printer_id=printer_id,
                printer_name=printer_name,
                options=options,
                requested_by_user_id=requested_by_user_id,
                requested_by_username=requested_by_username,
                project_id=project_id,
                queue_item_id=queue_item_id,
            )
            self._next_job_id += 1
            self._active_jobs[job.id] = ActiveDispatchState(job=job, message=f"Queue dispatch to {printer_name}...")
            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "dispatched",
                    "job_id": job.id,
                    "source_name": source_name,
                    "printer_id": printer_id,
                    "printer_name": printer_name,
                    "message": f"Queue dispatching to {printer_name}",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

        try:
            await self._process_job(job)
        except DispatchJobCancelled:
            pass  # outcome.cancelled already set by the runner
        except Exception:
            # outcome.error already set; logged inside runner
            pass
        finally:
            async with self._lock:
                self._active_jobs.pop(job.id, None)
                done_payload = self._build_state_payload_unlocked(
                    recent_event={
                        "status": "completed" if job.outcome.get("success") else "failed",
                        "job_id": job.id,
                        "source_name": source_name,
                        "printer_id": printer_id,
                        "printer_name": printer_name,
                        "message": job.outcome.get("error") or "done",
                    }
                )
            await ws_manager.broadcast({"type": "background_dispatch", "data": done_payload})

        return dict(job.outcome)

    async def dispatch_print_library_file(
        self,
        *,
        file_id: int,
        filename: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        cleanup_library_after_dispatch: bool = False,
    ) -> dict[str, Any]:
        return await self._dispatch(
            kind="print_library_file",
            source_id=file_id,
            source_name=filename,
            printer_id=printer_id,
            printer_name=printer_name,
            options=options,
            requested_by_user_id=requested_by_user_id,
            requested_by_username=requested_by_username,
            project_id=project_id,
            cleanup_library_after_dispatch=cleanup_library_after_dispatch,
        )

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """Cancel a queued dispatch job.

        Queued jobs are removed immediately. Active jobs are cancelled
        cooperatively and will stop at the next cancellation checkpoint.
        """
        async with self._lock:
            # Check active jobs first
            active_state = self._active_jobs.get(job_id)
            if active_state is not None:
                logger.info("Cancel requested for active dispatch job %s", job_id)
                self._cancel_requested_job_ids.add(job_id)
                active_job = active_state.job
                payload = self._build_state_payload_unlocked(
                    recent_event={
                        "status": "cancelling",
                        "job_id": active_job.id,
                        "source_name": active_job.source_name,
                        "printer_id": active_job.printer_id,
                        "printer_name": active_job.printer_name,
                        "message": "Cancelling current dispatch...",
                    }
                )
                result = {
                    "cancelled": True,
                    "pending": True,
                    "job_id": active_job.id,
                    "source_name": active_job.source_name,
                    "printer_id": active_job.printer_id,
                    "printer_name": active_job.printer_name,
                }
                await ws_manager.broadcast({"type": "background_dispatch", "data": payload})
                return result

            # Check queued jobs
            cancelled_job: PrintDispatchJob | None = None
            for job in self._queued_jobs:
                if job.id == job_id:
                    cancelled_job = job
                    break

            if not cancelled_job:
                logger.info("Cancel requested for unknown dispatch job %s", job_id)
                return {"cancelled": False, "reason": "not_found"}

            self._queued_jobs.remove(cancelled_job)
            logger.info("Cancelled queued dispatch job %s", cancelled_job.id)
            self._batch_total = max(0, self._batch_total - 1)

            if self._batch_total == 0 and len(self._queued_jobs) == 0 and len(self._active_jobs) == 0:
                self._batch_completed = 0
                self._batch_failed = 0

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "cancelled",
                    "job_id": cancelled_job.id,
                    "source_name": cancelled_job.source_name,
                    "printer_id": cancelled_job.printer_id,
                    "printer_name": cancelled_job.printer_name,
                    "message": "Cancelled from queue",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})
        return {
            "cancelled": True,
            "pending": False,
            "job_id": cancelled_job.id,
            "source_name": cancelled_job.source_name,
            "printer_id": cancelled_job.printer_id,
            "printer_name": cancelled_job.printer_name,
        }

    async def _dispatch(
        self,
        *,
        kind: Literal["reprint_archive", "print_library_file"],
        source_id: int,
        source_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        cleanup_library_after_dispatch: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            has_pending_for_printer = any(job.printer_id == printer_id for job in self._queued_jobs)
            has_active_for_printer = any(active.job.printer_id == printer_id for active in self._active_jobs.values())

            if has_pending_for_printer or has_active_for_printer:
                raise DispatchEnqueueRejected(f"Printer {printer_name} already has a background dispatch in progress")

            if self._printer_is_busy_printing(printer_id):
                raise DispatchEnqueueRejected(f"Printer {printer_name} is currently busy printing")

            dispatch_position = len(self._queued_jobs) + len(self._active_jobs) + 1
            job = PrintDispatchJob(
                id=self._next_job_id,
                kind=kind,
                source_id=source_id,
                source_name=source_name,
                printer_id=printer_id,
                printer_name=printer_name,
                options=options,
                requested_by_user_id=requested_by_user_id,
                requested_by_username=requested_by_username,
                project_id=project_id,
                cleanup_library_after_dispatch=cleanup_library_after_dispatch,
            )
            self._next_job_id += 1
            self._batch_total += 1
            self._queued_jobs.append(job)
            self._job_event.set()

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "dispatched",
                    "job_id": job.id,
                    "source_name": source_name,
                    "printer_id": printer_id,
                    "printer_name": printer_name,
                    "message": f"Dispatched to {printer_name}",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

        return {
            "dispatch_job_id": job.id,
            "dispatch_position": dispatch_position,
            "status": "dispatched",
            "printer_id": printer_id,
            "source_id": source_id,
            "source_name": source_name,
        }

    async def _dispatcher_loop(self):
        while True:
            await self._job_event.wait()
            self._job_event.clear()

            while True:
                payload: dict[str, Any] | None = None
                job_to_start: PrintDispatchJob | None = None
                async with self._lock:
                    # Multiple jobs can be active concurrently. Mutual
                    # exclusion of the *startup* (DB-write) phase is
                    # enforced inside ``_run_*`` via ``self._startup_lock``;
                    # the FTP / start_print / post-write phases run in
                    # parallel across printers.
                    if not self._queued_jobs:
                        break

                    job_to_start = self._queued_jobs.popleft()
                    self._active_jobs[job_to_start.id] = ActiveDispatchState(
                        job=job_to_start,
                        message="Preparing background dispatch...",
                    )

                    task = asyncio.create_task(
                        self._run_active_job(job_to_start), name=f"background-dispatch-job-{job_to_start.id}"
                    )
                    self._running_tasks[job_to_start.id] = task

                    payload = self._build_state_payload_unlocked(
                        recent_event={
                            "status": "processing",
                            "job_id": job_to_start.id,
                            "source_name": job_to_start.source_name,
                            "printer_id": job_to_start.printer_id,
                            "printer_name": job_to_start.printer_name,
                            "message": "Preparing background dispatch...",
                        }
                    )

                if payload:
                    await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    async def _run_active_job(self, job: PrintDispatchJob):
        try:
            await self._process_job(job)
            await self._mark_job_finished(job, failed=False, message="Background dispatch complete")
        except DispatchJobCancelled:
            await self._mark_job_cancelled(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Background dispatch job %s failed: %s", job.id, e, exc_info=True)
            await self._mark_job_finished(job, failed=True, message=str(e))
        finally:
            self._job_event.set()

    async def _build_injection_spec(
        self,
        *,
        job: PrintDispatchJob,
        printer_model: str | None,
        plate_id: int,
    ) -> GcodeInjectionSpec | None:
        """Resolve the per-job injection spec from settings + per-printer model (#422).

        Returns a ``GcodeInjectionSpec`` for ``apply_3mf_transforms`` to splice
        in during its single open/write pass, or None when injection is off,
        the printer model is unknown, or no snippets are configured for the
        target model. The actual zip mutation lives in ``apply_3mf_transforms``
        so M970-commenting and snippet-injection share one open/repack cycle
        instead of two — important on multi-plate 50+ MB 3MFs.
        """
        if not job.options.get("gcode_injection"):
            return None
        if not printer_model:
            logger.info("Dispatch job %s: gcode_injection on but no printer model, skipping", job.id)
            return None
        try:
            import json as _json

            from backend.app.api.routes.settings import get_setting

            async with self._session_factory() as _sdb:
                snippets_raw = await get_setting(_sdb, "gcode_snippets")
            if not snippets_raw:
                return None
            snippets = _json.loads(snippets_raw)
            model_snippets = snippets.get(printer_model, {}) if isinstance(snippets, dict) else {}
            start_gc = (model_snippets.get("start_gcode") or "").strip()
            end_gc = (model_snippets.get("end_gcode") or "").strip()
            if not start_gc and not end_gc:
                return None
            return GcodeInjectionSpec(
                plate_id=plate_id,
                start_gcode=start_gc or None,
                end_gcode=end_gc or None,
            )
        except Exception as exc:
            logger.warning("Dispatch job %s: failed to resolve gcode_snippets (%s), skipping", job.id, exc)
            return None

    async def _set_active_message(self, job: PrintDispatchJob, message: str):
        async with self._lock:
            active = self._active_jobs.get(job.id)
            if not active:
                return
            active.message = message
            # New phase → previous upload progress is no longer relevant.
            # Without this the toast keeps rendering a 100% progress bar
            # during post-upload phases (swap macros, "Starting print…")
            # because ``_set_active_upload_progress(job, 1, 1)`` runs
            # right after upload finishes and nothing ever clears it.
            # The next upload (if any) will repopulate via the same setter.
            active.upload_bytes = None
            active.upload_total_bytes = None
            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "processing",
                    "job_id": active.job.id,
                    "source_name": active.job.source_name,
                    "printer_id": active.job.printer_id,
                    "printer_name": active.job.printer_name,
                    "message": message,
                }
            )
        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    async def _set_active_upload_progress(self, job: PrintDispatchJob, uploaded: int, total: int):
        async with self._lock:
            active = self._active_jobs.get(job.id)
            if not active:
                return

            active.upload_bytes = max(0, int(uploaded))
            active.upload_total_bytes = max(0, int(total))
            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "processing",
                    "job_id": active.job.id,
                    "source_name": active.job.source_name,
                    "printer_id": active.job.printer_id,
                    "printer_name": active.job.printer_name,
                    "message": active.message,
                }
            )
        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    async def _mark_job_finished(self, job: PrintDispatchJob, *, failed: bool, message: str):
        async with self._lock:
            if failed:
                self._batch_failed += 1
            else:
                self._batch_completed += 1

            self._active_jobs.pop(job.id, None)
            self._running_tasks.pop(job.id, None)
            self._cancel_requested_job_ids.discard(job.id)

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "failed" if failed else "completed",
                    "job_id": job.id,
                    "source_name": job.source_name,
                    "printer_id": job.printer_id,
                    "printer_name": job.printer_name,
                    "message": message,
                }
            )
            should_reset_batch = len(self._queued_jobs) == 0 and len(self._active_jobs) == 0

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

        if should_reset_batch:
            async with self._lock:
                if len(self._queued_jobs) == 0 and len(self._active_jobs) == 0:
                    self._batch_total = 0
                    self._batch_completed = 0
                    self._batch_failed = 0

    async def _mark_job_cancelled(self, job: PrintDispatchJob):
        async with self._lock:
            self._active_jobs.pop(job.id, None)
            self._running_tasks.pop(job.id, None)
            self._cancel_requested_job_ids.discard(job.id)
            self._batch_total = max(0, self._batch_total - 1)

            if self._batch_total == 0 and len(self._queued_jobs) == 0 and len(self._active_jobs) == 0:
                self._batch_completed = 0
                self._batch_failed = 0

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "cancelled",
                    "job_id": job.id,
                    "source_name": job.source_name,
                    "printer_id": job.printer_id,
                    "printer_name": job.printer_name,
                    "message": "Cancelled during dispatch",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    def _is_cancel_requested(self, job_id: int) -> bool:
        return job_id in self._cancel_requested_job_ids

    def _raise_if_cancel_requested(self, job: PrintDispatchJob):
        if self._is_cancel_requested(job.id):
            raise DispatchJobCancelled(f"Dispatch job {job.id} cancelled")

    def _build_state_payload_unlocked(self, recent_event: dict[str, Any] | None = None) -> dict[str, Any]:
        processing = len(self._active_jobs)
        dispatched = len(self._queued_jobs)

        dispatched_jobs = [
            {
                "job_id": job.id,
                "kind": job.kind,
                "source_id": job.source_id,
                "source_name": job.source_name,
                "printer_id": job.printer_id,
                "printer_name": job.printer_name,
            }
            for job in list(self._queued_jobs)
        ]

        active_jobs: list[dict[str, Any]] = []
        for active in self._active_jobs.values():
            upload_progress_pct = None
            if active.upload_total_bytes and active.upload_total_bytes > 0 and active.upload_bytes is not None:
                upload_progress_pct = round(
                    max(0.0, min(100.0, (active.upload_bytes / active.upload_total_bytes) * 100.0)), 1
                )

            active_jobs.append(
                {
                    "job_id": active.job.id,
                    "kind": active.job.kind,
                    "source_id": active.job.source_id,
                    "source_name": active.job.source_name,
                    "printer_id": active.job.printer_id,
                    "printer_name": active.job.printer_name,
                    "message": active.message,
                    "upload_bytes": active.upload_bytes,
                    "upload_total_bytes": active.upload_total_bytes,
                    "upload_progress_pct": upload_progress_pct,
                }
            )

        active_jobs.sort(key=lambda item: int(item["job_id"]))
        active_job = active_jobs[0] if active_jobs else None

        return {
            "total": self._batch_total,
            "dispatched": dispatched,
            "processing": processing,
            "completed": self._batch_completed,
            "failed": self._batch_failed,
            "dispatched_jobs": dispatched_jobs,
            "active_jobs": active_jobs,
            "active_job": active_job,
            "recent_event": recent_event,
        }

    async def _process_job(self, job: PrintDispatchJob):
        if job.kind == "reprint_archive":
            await self._run_reprint_archive(job)
            return
        if job.kind == "print_library_file":
            await self._run_print_library_file(job)
            return
        raise RuntimeError(f"Unknown dispatch job kind: {job.kind}")

    async def _run_reprint_archive(self, job: PrintDispatchJob):
        from backend.app.main import register_expected_print

        job.outcome = {"success": False, "archive_id": None, "error": None, "cancelled": False}

        async with async_session() as db:
            service = ArchiveService(db)
            archive = await service.get_archive(job.source_id)
            if not archive:
                raise RuntimeError("Archive not found")

            # Pre-stamp swap_macro_events_pending while we have a writable
            # session and BEFORE FTP upload / start_print / runtime-tracker
            # writes start competing for SQLite's single writer. Reprint
            # reuses the existing archive row, so this is the only chance
            # to persist the intent without a standalone UPDATE that
            # would race the runtime-tracker. Library-file dispatch
            # handles this via archive_print()'s INSERT instead.
            #
            # Hold the startup-lock only while the DB write txn is open;
            # releasing right after commit lets the next job begin its own
            # setup while this one runs FTP / start_print in parallel.
            await self._startup_lock.acquire()
            try:
                opts = job.options if isinstance(job.options, dict) else {}
                if opts.get("execute_swap_macros"):
                    _events = opts.get("swap_macro_events") or []
                    if "swap_mode_change_table" in _events:
                        _merged = dict(archive.extra_data or {})
                        _merged["swap_macro_events_pending"] = list(_events)
                        archive.extra_data = _merged
                        await db.commit()
            finally:
                self._startup_lock.release()

            printer = await db.scalar(select(Printer).where(Printer.id == job.printer_id))
            if not printer:
                raise RuntimeError("Printer not found")

            printer_name = printer.name
            printer_ip = printer.ip_address
            printer_access_code = printer.access_code
            printer_model = printer.model
            archive_filename = archive.filename

            if not printer_manager.is_connected(job.printer_id):
                raise RuntimeError("Printer is not connected")

            # re-Connect MQTT if stalled
            if not await printer_manager.ensure_fresh_connection_for_printer(printer):
                raise RuntimeError("Can`t re-connect printer MQTT")

            file_path = settings.base_dir / archive.file_path
            if not file_path.exists():
                raise RuntimeError("Archive file not found")

            # Unified 3MF post-processing: M970 commenting (mesh-mode-fast-check
            # off) and per-plate G-code injection (#422) share a single
            # open/mutate/write pass instead of unzipping+rezipping the file
            # twice. ``apply_3mf_transforms`` returns the source path unchanged
            # when no transform actually mutated any byte (e.g. an already-
            # patched Swaplist export, or an injection toggle without snippets
            # configured for this printer model).
            upload_file_path = file_path
            _patch_cleanup_dir = None
            inject_spec = await self._build_injection_spec(
                job=job,
                printer_model=printer_model,
                plate_id=archive.plate_id or 1,
            )
            if not job.options.get("mesh_mode_fast_check", True) or inject_spec is not None:
                from backend.app.services.gcode_patcher import apply_3mf_transforms

                patched_path, patches = await asyncio.to_thread(
                    apply_3mf_transforms,
                    file_path,
                    mesh_mode_fast_check_off=not job.options.get("mesh_mode_fast_check", True),
                    gcode_injection=inject_spec,
                )
                if patches and patched_path != file_path:
                    upload_file_path = patched_path
                    _patch_cleanup_dir = patched_path.parent
                    existing_patches = job.options.get("applied_patches") or []
                    job.options["applied_patches"] = existing_patches + patches
                    logger.info("Dispatch job %s: 3MF transformed (%s)", job.id, patches)

            base_name = archive.filename
            if base_name.endswith(".gcode.3mf"):
                base_name = base_name[:-10]
            elif base_name.endswith(".3mf"):
                base_name = base_name[:-4]
            remote_filename = f"{base_name}.3mf"
            # Sanitize: firmware parses ftp://{filename} as a URL, spaces break it
            remote_filename = remote_filename.replace(" ", "_")
            remote_path = f"/{remote_filename}"

            ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
            self._raise_if_cancel_requested(job)

            await self._set_active_message(job, f"Preparing upload to {printer_name}...")
            await delete_file_async(
                printer_ip,
                printer_access_code,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer_model,
            )

            # Clean up /cache/ - delete stale .3mf and .bbl files from previous prints
            sanitized_base = remote_filename[:-4] if remote_filename.endswith(".3mf") else remote_filename
            try:
                cache_files = await list_files_async(
                    printer_ip,
                    printer_access_code,
                    "/cache",
                    socket_timeout=ftp_timeout,
                    printer_model=printer_model,
                )
                for f in cache_files:
                    fname = f.get("name", "")
                    if f.get("is_dir"):
                        continue
                    if fname == remote_filename or fname.endswith(f"_{sanitized_base}.bbl"):
                        try:
                            await delete_file_async(
                                printer_ip,
                                printer_access_code,
                                f"/cache/{fname}",
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                            )
                            logger.info("Dispatch job %s: Deleted /cache/%s", job.id, fname)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Dispatch job %s: Cache cleanup failed (non-critical): %s", job.id, e)

            self._raise_if_cancel_requested(job)

            try:
                await self._set_active_message(job, f"Uploading {archive_filename} to {printer_name}...")
                loop = asyncio.get_running_loop()
                progress_state = {"last_emit": 0.0, "last_bytes": 0}

                def upload_progress_callback(uploaded: int, total: int):
                    if self._is_cancel_requested(job.id):
                        raise DispatchJobCancelled(f"Dispatch job {job.id} cancelled during upload")

                    now = time.monotonic()
                    should_emit = (
                        uploaded >= total
                        or now - progress_state["last_emit"] >= 0.2
                        or uploaded - progress_state["last_bytes"] >= 256 * 1024
                    )

                    if should_emit:
                        progress_state["last_emit"] = now
                        progress_state["last_bytes"] = uploaded
                        loop.call_soon_threadsafe(
                            lambda u=uploaded, t=total: asyncio.create_task(self._set_active_upload_progress(job, u, t))
                        )

                if ftp_retry_enabled:
                    uploaded = await with_ftp_retry(
                        upload_file_async,
                        printer_ip,
                        printer_access_code,
                        upload_file_path,
                        remote_path,
                        progress_callback=upload_progress_callback,
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                        max_retries=ftp_retry_count,
                        retry_delay=ftp_retry_delay,
                        operation_name=f"Upload for reprint to {printer_name}",
                        non_retry_exceptions=(DispatchJobCancelled,),
                    )
                else:
                    uploaded = await upload_file_async(
                        printer_ip,
                        printer_access_code,
                        upload_file_path,
                        remote_path,
                        progress_callback=upload_progress_callback,
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                    )

                # Clean up patched temp file after upload (original stays intact).
                if _patch_cleanup_dir:
                    import shutil

                    shutil.rmtree(_patch_cleanup_dir, ignore_errors=True)
                    _patch_cleanup_dir = None

                if uploaded:
                    await self._set_active_upload_progress(job, 1, 1)

                if not uploaded:
                    raise RuntimeError(
                        "Failed to upload file to printer. Check if SD card is inserted and properly formatted (FAT32/exFAT)."
                    )

                register_expected_print(
                    job.printer_id,
                    remote_filename,
                    job.source_id,
                    ams_mapping=job.options.get("ams_mapping"),
                )

                plate_id = self._resolve_plate_id(file_path, job.options.get("plate_id"))

                self._raise_if_cancel_requested(job)

                # Strict stagger check (optional, off by default): if enabled,
                # refuse to start if no free slot, so Print Now respects the
                # grid-load cap just like queue-driven dispatches.
                try:
                    from backend.app.api.routes.settings import get_setting
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _strict_raw = await get_setting(_sdb, "stagger_strict_for_direct_dispatch")
                        _stagger_enabled, _stagger_concurrent, _, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if (
                        _stagger_enabled
                        and (_strict_raw or "false").lower() == "true"
                        and not print_scheduler._can_start_staggered(_stagger_concurrent)
                    ):
                        raise RuntimeError(
                            "Stagger cap reached — wait for a free slot or disable stagger_strict_for_direct_dispatch"
                        )
                except RuntimeError:
                    raise
                except Exception as _e:
                    logger.debug("Strict stagger check failed (non-fatal): %s", _e)

                # Swap-mode start macro — fires before the print starts.
                await self._run_swap_macro_if_needed(
                    job, printer, "swap_mode_start", f"Running swap start macro on {printer_name}..."
                )

                # Tick swap_mode_start off the pending checklist now that
                # it actually fired. Keeps extra_data["swap_macro_events_pending"]
                # honest as a "what's still to do" list (variant 2 — proper
                # checklist). Safe to write here: macro completed, start_print
                # hasn't fired yet → runtime-tracker isn't producing writes.
                from backend.app.services.archive import remove_swap_pending_event

                if archive and remove_swap_pending_event(archive, "swap_mode_start"):
                    await db.commit()

                await self._set_active_message(job, f"Starting print on {printer_name}...")
                started = printer_manager.start_print(
                    job.printer_id,
                    remote_filename,
                    plate_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    timelapse=job.options.get("timelapse", False),
                    bed_levelling=job.options.get("bed_levelling", True),
                    flow_cali=job.options.get("flow_cali", False),
                    layer_inspect=job.options.get("layer_inspect", False),
                    use_ams=job.options.get("use_ams", True),
                )

                if not started:
                    await self._cleanup_sd_card_file(
                        printer_ip,
                        printer_access_code,
                        remote_path,
                        printer_model,
                    )
                    raise RuntimeError("Failed to start print")

                # Wait for the printer to actually pick up the command before
                # marking the dispatch job complete (#1042/#1134). MQTT-publish
                # success only proves the command queued locally; the printer
                # can still reject it (HMS error pending, half-broken session,
                # SD card missing) and never transition. Until #1134 this
                # watchdog was fire-and-forget — the job was reported
                # successful and the user had no signal that the print never
                # started. The uploaded file is intentionally left on the
                # printer's SD card on timeout: the next dispatch will
                # overwrite it via the existing delete-then-upload step, and
                # the printer may still be in the middle of reading it if it
                # picked up just past the timeout.
                _post_status = printer_manager.get_status(job.printer_id)
                pre_state = getattr(_post_status, "state", None)
                pre_subtask_id = getattr(_post_status, "subtask_id", None)
                pre_gcode_file = getattr(_post_status, "gcode_file", None)
                if pre_state:
                    await self._set_active_message(job, f"Waiting for {printer_name} to acknowledge print...")
                    transitioned = await self._verify_print_response(
                        job.printer_id,
                        printer_name,
                        pre_state,
                        pre_subtask_id=pre_subtask_id,
                        pre_gcode_file=pre_gcode_file,
                    )
                    if not transitioned:
                        raise RuntimeError(
                            f"Printer did not acknowledge print command — state still {pre_state}. "
                            f"Check the printer for a pending error (HMS code, plate-clear prompt, "
                            f"SD card) and try again."
                        )

                # Register in-memory swap config for on_print_complete's fast
                # path. Persistence to archive.extra_data (restart recovery)
                # is handled where the archive row is created / loaded — see
                # archive_print's swap_macro_events_pending parameter for the
                # library-file path, and the explicit pre-stamp block below
                # the archive lookup for the reprint path.
                from backend.app.main import register_swap_config

                register_swap_config(
                    job.printer_id,
                    job.options if isinstance(job.options, dict) else {},
                )

                # Register stagger slot so subsequent queue-driven
                # dispatches respect the grid-load cap.  Uses system-wide
                # default interval; per-printer override is queue-only.
                try:
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _stagger_enabled, _, _stagger_interval, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if _stagger_enabled:
                        print_scheduler._register_stagger_start(job.printer_id, _stagger_interval)
                except Exception as _e:
                    logger.debug("Stagger registration for direct dispatch failed: %s", _e)

                if job.requested_by_user_id and job.requested_by_username:
                    printer_manager.set_current_print_user(
                        job.printer_id,
                        job.requested_by_user_id,
                        job.requested_by_username,
                    )

                job.outcome = {"success": True, "archive_id": archive.id, "error": None, "cancelled": False}
            except DispatchJobCancelled:
                await self._set_active_message(job, f"Cancelled upload on {printer_name}.")
                job.outcome = {"success": False, "archive_id": None, "error": "Cancelled", "cancelled": True}
                raise
            except Exception as e:
                job.outcome = {"success": False, "archive_id": None, "error": str(e), "cancelled": False}
                raise
            finally:
                job.completion_event.set()

    async def _run_swap_macro_if_needed(
        self,
        job: PrintDispatchJob,
        printer,
        event: str,
        status_message: str,
    ):
        """Execute a swap macro if the job's options request it for *event*.

        Raises ``RuntimeError`` on failure so the dispatch job aborts.
        """
        opts = job.options if isinstance(job.options, dict) else {}
        if not opts.get("execute_swap_macros"):
            return
        events = opts.get("swap_macro_events") or []
        if event not in events:
            return

        from backend.app.core.database import async_session
        from backend.app.services.macro_executor import find_swap_macro

        async with async_session() as db:
            macro = await find_swap_macro(db, event, printer)

        if not macro or not macro.gcode:
            logger.info(
                "Dispatch job %s: no gcode for swap event '%s' on printer %s — skipping",
                job.id,
                event,
                printer.name,
            )
            return

        await self._set_active_message(job, status_message)
        success, msg = await printer_manager.execute_macro_and_wait(job.printer_id, macro.gcode, macro.name)
        if not success:
            raise RuntimeError(f"Swap macro '{macro.name}' failed: {msg}")

    async def _run_print_library_file(self, job: PrintDispatchJob):
        from backend.app.main import register_expected_print

        # Seeded in case any early branch raises — keeps the outcome shape
        # consistent for queue-item callers awaiting completion_event.
        job.outcome = {"success": False, "archive_id": None, "error": None, "cancelled": False}

        async with async_session() as db:
            lib_file = await db.scalar(LibraryFile.active().where(LibraryFile.id == job.source_id))
            if not lib_file:
                raise RuntimeError("File not found")

            if not self._is_sliced_file(lib_file.filename):
                raise RuntimeError("Not a sliced file. Only .gcode or .gcode.3mf files can be printed.")

            file_path = Path(settings.base_dir) / lib_file.file_path
            if not file_path.exists():
                raise RuntimeError("File not found on disk")

            printer = await db.scalar(select(Printer).where(Printer.id == job.printer_id))
            if not printer:
                raise RuntimeError("Printer not found")

            printer_name = printer.name
            printer_ip = printer.ip_address
            printer_access_code = printer.access_code
            printer_model = printer.model
            library_filename = lib_file.filename

            if not printer_manager.is_connected(job.printer_id):
                raise RuntimeError("Printer is not connected")

            # re-Connect MQTT if stalled
            if not await printer_manager.ensure_fresh_connection_for_printer(printer):
                raise RuntimeError("Can`t re-connect printer MQTT")

            # Unified 3MF post-processing — same single-pass pipeline as the
            # archive path above. See _maybe_inject_gcode → _build_injection_spec.
            upload_file_path = file_path
            _patch_cleanup_dir_lib = None
            inject_spec_lib = await self._build_injection_spec(
                job=job,
                printer_model=printer_model,
                plate_id=int(job.options.get("plate_id") or 1),
            )
            if not job.options.get("mesh_mode_fast_check", True) or inject_spec_lib is not None:
                from backend.app.services.gcode_patcher import apply_3mf_transforms

                patched_path, patches = await asyncio.to_thread(
                    apply_3mf_transforms,
                    file_path,
                    mesh_mode_fast_check_off=not job.options.get("mesh_mode_fast_check", True),
                    gcode_injection=inject_spec_lib,
                )
                if patches and patched_path != file_path:
                    upload_file_path = patched_path
                    _patch_cleanup_dir_lib = patched_path.parent
                    existing_patches = job.options.get("applied_patches") or []
                    job.options["applied_patches"] = existing_patches + patches
                    logger.info("Dispatch job %s: 3MF transformed (%s)", job.id, patches)

            await self._set_active_message(job, f"Creating archive for {lib_file.filename}...")
            # Hold the startup-lock for the DB-write critical section only:
            # ``archive_print`` (heavy INSERT into print_archives + related
            # rows) plus the queue-item linking. Commit closes the txn
            # before FTP starts, so two parallel jobs no longer race on
            # SQLite's single-writer lock during a held FTP session. The
            # finally-block guarantees release on any exception path.
            await self._startup_lock.acquire()
            try:
                archive_service = ArchiveService(db)
                applied_patches = job.options.get("applied_patches") if isinstance(job.options, dict) else None
                # Archive the file that will ACTUALLY be sent to the printer
                # (patched or original). content_hash will match what lands on
                # SD so on_print_complete chain-lookup works even if
                # _expected_prints misses.  source_content_hash stays the
                # library's original hash for design-level dedup.
                archive = await archive_service.archive_print(
                    printer_id=job.printer_id,
                    source_file=upload_file_path,
                    original_filename=lib_file.filename,
                    project_id=job.project_id,
                    source_content_hash=lib_file.file_hash,
                    applied_patches=applied_patches or None,
                    library_file_id=lib_file.id,
                    # Forward the requesting user so per-user stats filter sees this
                    # archive and the post-print notification has a recipient. Prior
                    # to upstream #276a1db3 all library-print archives landed with
                    # created_by_id=NULL regardless of who clicked Print.
                    created_by_id=job.requested_by_user_id,
                    # Born in "printing" so the UI doesn't flash a transient
                    # "archived" label during the FTP/MQTT window (#876 follow-up).
                    # Error paths below flip it to "failed" before the txn commits.
                    print_data={"status": "printing"},
                    # Persist swap intent in the same INSERT (post-start_print
                    # UPDATE raced the runtime-tracker on SQLite's single
                    # writer and timed out). The marker is only meaningful for
                    # ``on_print_complete``'s restart-recovery branch — fast
                    # path still uses ``_active_swap_config`` set by
                    # ``register_swap_config`` after start_print.
                    swap_macro_events_pending=(
                        job.options.get("swap_macro_events")
                        if isinstance(job.options, dict) and job.options.get("execute_swap_macros")
                        else None
                    ),
                )
                if not archive:
                    raise RuntimeError("Failed to create archive")

                # Queue-item dispatches: keep queue_item + archive aligned in the
                # same txn so the scheduler's follow-up logic sees a consistent
                # view. Also copies queue_id + batch_id onto the archive so the
                # archive-driven queue counters post-m019 can find this row.
                if job.queue_item_id:
                    from backend.app.models.print_queue import PrintQueueItem

                    q_item = await db.get(PrintQueueItem, job.queue_item_id)
                    if q_item is not None:
                        q_item.archive_id = archive.id
                        archive.queue_id = q_item.queue_id
                        archive.batch_id = q_item.batch_id

                # For non-queue dispatches (Print Now qty=1), attribute the
                # archive to the printer's default queue so GET /printer-queues/
                # counters include it.
                if archive.queue_id is None and job.printer_id is not None:
                    from backend.app.models.printer_queue import PrinterQueue as _PQ

                    archive.queue_id = (
                        await db.execute(select(_PQ.id).where(_PQ.printer_id == job.printer_id))
                    ).scalar_one_or_none()

                # Commit closes the write txn — was a flush() before, which
                # left an open txn that other jobs' archive_print INSERTs
                # contended on through the entire FTP upload window.
                await db.commit()
            finally:
                self._startup_lock.release()

            base_name = lib_file.filename
            if base_name.endswith(".gcode.3mf"):
                base_name = base_name[:-10]
            elif base_name.endswith(".3mf"):
                base_name = base_name[:-4]
            remote_filename = f"{base_name}.3mf"
            # Sanitize: firmware parses ftp://{filename} as a URL, spaces break it
            remote_filename = remote_filename.replace(" ", "_")
            remote_path = f"/{remote_filename}"

            ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
            self._raise_if_cancel_requested(job)

            await self._set_active_message(job, f"Preparing upload to {printer_name}...")
            await delete_file_async(
                printer_ip,
                printer_access_code,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer_model,
            )

            # Clean up /cache/ - delete stale .3mf and .bbl files from previous prints
            sanitized_base = remote_filename[:-4] if remote_filename.endswith(".3mf") else remote_filename
            try:
                cache_files = await list_files_async(
                    printer_ip,
                    printer_access_code,
                    "/cache",
                    socket_timeout=ftp_timeout,
                    printer_model=printer_model,
                )
                for f in cache_files:
                    fname = f.get("name", "")
                    if f.get("is_dir"):
                        continue
                    if fname == remote_filename or fname.endswith(f"_{sanitized_base}.bbl"):
                        try:
                            await delete_file_async(
                                printer_ip,
                                printer_access_code,
                                f"/cache/{fname}",
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                            )
                            logger.info("Dispatch job %s: Deleted /cache/%s", job.id, fname)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Dispatch job %s: Cache cleanup failed (non-critical): %s", job.id, e)

            self._raise_if_cancel_requested(job)

            try:
                await self._set_active_message(job, f"Uploading {library_filename} to {printer_name}...")
                loop = asyncio.get_running_loop()
                progress_state = {"last_emit": 0.0, "last_bytes": 0}

                def upload_progress_callback(uploaded: int, total: int):
                    if self._is_cancel_requested(job.id):
                        raise DispatchJobCancelled(f"Dispatch job {job.id} cancelled during upload")

                    now = time.monotonic()
                    should_emit = (
                        uploaded >= total
                        or now - progress_state["last_emit"] >= 0.2
                        or uploaded - progress_state["last_bytes"] >= 256 * 1024
                    )

                    if should_emit:
                        progress_state["last_emit"] = now
                        progress_state["last_bytes"] = uploaded
                        loop.call_soon_threadsafe(
                            lambda u=uploaded, t=total: asyncio.create_task(self._set_active_upload_progress(job, u, t))
                        )

                if ftp_retry_enabled:
                    uploaded = await with_ftp_retry(
                        upload_file_async,
                        printer_ip,
                        printer_access_code,
                        upload_file_path,
                        remote_path,
                        progress_callback=upload_progress_callback,
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                        max_retries=ftp_retry_count,
                        retry_delay=ftp_retry_delay,
                        operation_name=f"Upload for print to {printer_name}",
                        non_retry_exceptions=(DispatchJobCancelled,),
                    )
                else:
                    uploaded = await upload_file_async(
                        printer_ip,
                        printer_access_code,
                        upload_file_path,
                        remote_path,
                        progress_callback=upload_progress_callback,
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                    )

                # Clean up patched temp file after upload.
                if _patch_cleanup_dir_lib:
                    import shutil

                    shutil.rmtree(_patch_cleanup_dir_lib, ignore_errors=True)
                    _patch_cleanup_dir_lib = None

                if uploaded:
                    await self._set_active_upload_progress(job, 1, 1)

                if not uploaded:
                    await db.rollback()
                    raise RuntimeError(
                        "Failed to upload file to printer. Check if SD card is inserted and properly formatted (FAT32/exFAT)."
                    )

                register_expected_print(
                    job.printer_id,
                    remote_filename,
                    archive.id,
                    ams_mapping=job.options.get("ams_mapping"),
                )

                plate_id = self._resolve_plate_id(file_path, job.options.get("plate_id"))

                self._raise_if_cancel_requested(job)

                # Strict stagger check (optional, off by default): if enabled,
                # refuse to start if no free slot, so Print Now respects the
                # grid-load cap just like queue-driven dispatches.
                try:
                    from backend.app.api.routes.settings import get_setting
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _strict_raw = await get_setting(_sdb, "stagger_strict_for_direct_dispatch")
                        _stagger_enabled, _stagger_concurrent, _, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if (
                        _stagger_enabled
                        and (_strict_raw or "false").lower() == "true"
                        and not print_scheduler._can_start_staggered(_stagger_concurrent)
                    ):
                        raise RuntimeError(
                            "Stagger cap reached — wait for a free slot or disable stagger_strict_for_direct_dispatch"
                        )
                except RuntimeError:
                    raise
                except Exception as _e:
                    logger.debug("Strict stagger check failed (non-fatal): %s", _e)

                # Swap-mode start macro — fires before the print starts.
                await self._run_swap_macro_if_needed(
                    job, printer, "swap_mode_start", f"Running swap start macro on {printer_name}..."
                )

                # Tick swap_mode_start off the pending checklist now that
                # it actually fired. Keeps extra_data["swap_macro_events_pending"]
                # honest as a "what's still to do" list (variant 2 — proper
                # checklist). Safe to write here: macro completed, start_print
                # hasn't fired yet → runtime-tracker isn't producing writes.
                from backend.app.services.archive import remove_swap_pending_event

                if archive and remove_swap_pending_event(archive, "swap_mode_start"):
                    await db.commit()

                await self._set_active_message(job, f"Starting print on {printer_name}...")
                started = printer_manager.start_print(
                    job.printer_id,
                    remote_filename,
                    plate_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    timelapse=job.options.get("timelapse", False),
                    bed_levelling=job.options.get("bed_levelling", True),
                    flow_cali=job.options.get("flow_cali", False),
                    layer_inspect=job.options.get("layer_inspect", False),
                    use_ams=job.options.get("use_ams", True),
                )

                if not started:
                    await self._cleanup_sd_card_file(
                        printer_ip,
                        printer_access_code,
                        remote_path,
                        printer_model,
                    )
                    await db.rollback()
                    raise RuntimeError("Failed to start print")

                # Register in-memory swap config for on_print_complete's fast
                # path. Persistence to archive.extra_data (restart recovery)
                # is handled where the archive row is created / loaded — see
                # archive_print's swap_macro_events_pending parameter for the
                # library-file path, and the explicit pre-stamp block below
                # the archive lookup for the reprint path.
                from backend.app.main import register_swap_config

                register_swap_config(
                    job.printer_id,
                    job.options if isinstance(job.options, dict) else {},
                )

                # Register stagger slot so subsequent queue-driven
                # dispatches respect the grid-load cap.  Uses system-wide
                # default interval; per-printer override is queue-only.
                try:
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _stagger_enabled, _, _stagger_interval, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if _stagger_enabled:
                        print_scheduler._register_stagger_start(job.printer_id, _stagger_interval)
                except Exception as _e:
                    logger.debug("Stagger registration for direct dispatch failed: %s", _e)

                # See _run_reprint_archive for rationale (#1042/#1134). Outer
                # ``except Exception`` block already runs
                # ``_mark_dispatch_archive_terminal(archive.id, "failed", ...)``
                # so a RuntimeError raised here flips the freshly-created
                # archive from "printing" → "failed" without leaving a
                # phantom row for a print that never started.
                _post_status = printer_manager.get_status(job.printer_id)
                pre_state = getattr(_post_status, "state", None)
                pre_subtask_id = getattr(_post_status, "subtask_id", None)
                pre_gcode_file = getattr(_post_status, "gcode_file", None)
                if pre_state:
                    await self._set_active_message(job, f"Waiting for {printer_name} to acknowledge print...")
                    transitioned = await self._verify_print_response(
                        job.printer_id,
                        printer_name,
                        pre_state,
                        pre_subtask_id=pre_subtask_id,
                        pre_gcode_file=pre_gcode_file,
                    )
                    if not transitioned:
                        raise RuntimeError(
                            f"Printer did not acknowledge print command — state still {pre_state}. "
                            f"Check the printer for a pending error (HMS code, plate-clear prompt, "
                            f"SD card) and try again."
                        )

                # Register the requesting user so per-user stats filter sees
                # this print and the post-print notification has a recipient.
                # Mirrors the reprint path above — prior to upstream #276a1db3
                # the library-print branch skipped this call even though the
                # user was plumbed into the job object.
                if job.requested_by_user_id and job.requested_by_username:
                    printer_manager.set_current_print_user(
                        job.printer_id,
                        job.requested_by_user_id,
                        job.requested_by_username,
                    )

                # Direct-Print flow only: archive_print copies the 3MF, so
                # deleting the transient library row + files here leaves the
                # archive intact. Staged in the same transaction as everything
                # else — a mid-flight FTP / start_print failure rolls both
                # archive creation and library deletion back cleanly. Disk
                # deletes run AFTER commit so a rollback leaves no orphan
                # library_file row pointing at a file we already unlinked.
                # External library files (is_external=True) are never touched.
                # Upstream #730 / #1682b695.
                cleanup_disk_paths: list[Path] = []
                if job.cleanup_library_after_dispatch and not lib_file.is_external:
                    cleanup_disk_paths.append(Path(settings.base_dir) / lib_file.file_path)
                    if lib_file.thumbnail_path:
                        thumb_path = Path(lib_file.thumbnail_path)
                        if not thumb_path.is_absolute():
                            thumb_path = Path(settings.base_dir) / lib_file.thumbnail_path
                        cleanup_disk_paths.append(thumb_path)
                    await db.delete(lib_file)

                await db.commit()

                for cleanup_path in cleanup_disk_paths:
                    try:
                        if cleanup_path.exists():
                            cleanup_path.unlink()
                    except OSError as cleanup_err:
                        logger.warning(
                            "Failed to delete transient library file %s: %s",
                            cleanup_path,
                            cleanup_err,
                        )

                job.outcome = {"success": True, "archive_id": archive.id, "error": None, "cancelled": False}
            except DispatchJobCancelled:
                await db.rollback()
                await self._set_active_message(job, f"Cancelled upload on {printer_name}.")
                # archive_print committed the row before this branch, so the
                # outer session rollback can't undo it. Flip the zombie from
                # "printing" → "cancelled" in a fresh session so the UI
                # doesn't keep it spinning forever.
                await self._mark_dispatch_archive_terminal(archive.id, "cancelled", "Cancelled before start")
                job.outcome = {"success": False, "archive_id": archive.id, "error": "Cancelled", "cancelled": True}
                raise
            except Exception as e:
                await self._mark_dispatch_archive_terminal(archive.id, "failed", str(e))
                job.outcome = {"success": False, "archive_id": archive.id, "error": str(e), "cancelled": False}
                raise
            finally:
                job.completion_event.set()

    @staticmethod
    async def _verify_print_response(
        printer_id: int,
        printer_name: str,
        pre_state: str,
        pre_subtask_id: str | None = None,
        timeout: float = 90.0,
        poll_interval: float = 3.0,
        pre_gcode_file: str | None = None,
    ) -> bool:
        """Wait for the printer to acknowledge a print command.

        Returns True if the printer transitioned (state advanced past
        ``pre_state`` or ``subtask_id`` advanced past ``pre_subtask_id``).
        Returns False on timeout — in that case logs a warning and (when the
        ``gcode_file`` discriminator says the publish didn't land) forces an
        MQTT reconnect, mirroring the queue-side watchdog
        (`_watchdog_print_start`). Caller surfaces the False result to the
        user (typically by raising so the dispatch job is marked failed).

        H2D can sit at FINISH for ~50 s after accepting `project_file` before
        flipping to PREPARE; the printer echoes our per-dispatch identity
        back as ``subtask_id`` on ``push_status`` first, so a subtask_id
        change is a definitive "command landed" signal even while state is
        still FINISH (#1078).
        """
        deadline = time.monotonic() + timeout
        last_status = None  # captured for #1150 gcode_file discriminator on timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            state = printer_manager.get_status(printer_id)
            if not state:
                # Printer momentarily not reporting — could be a brief MQTT
                # disconnect mid-window. Keep polling rather than declaring
                # failure on the first missed tick; the printer may reconnect
                # within the remaining timeout and still surface a transition.
                continue
            last_status = state
            if state.state != pre_state:
                return True
            if pre_subtask_id is not None and state.subtask_id is not None and state.subtask_id != pre_subtask_id:
                return True
        logger.warning(
            "Printer %s (%d) did not respond to print command within %.0fs "
            "(state still %s, subtask_id still %s) — printer may need restart",
            printer_name,
            printer_id,
            timeout,
            pre_state,
            pre_subtask_id,
        )
        # P1P 0500_4003 discriminator (#1150): if `gcode_file` advanced from
        # what we observed pre-dispatch, the printer accepted our project_file
        # and is just slow-parsing on the SD-card MCU side. Forcing an MQTT
        # reconnect mid-parse triggers 0500_4003. Only reconnect when
        # `gcode_file` is unchanged — that's the half-broken-publish signal
        # from #887 / #936.
        current_gcode_file = getattr(last_status, "gcode_file", None) if last_status else None
        publish_landed = current_gcode_file is not None and current_gcode_file != pre_gcode_file
        if publish_landed:
            logger.warning(
                "Printer %s (%d): gcode_file changed to %r (was %r) — printer "
                "received the command and is parsing slowly. Skipping forced "
                "MQTT reconnect to avoid 0500_4003 mid-parse (#1150).",
                printer_name,
                printer_id,
                current_gcode_file,
                pre_gcode_file,
            )
            return False
        client = printer_manager.get_client(printer_id)
        if client and hasattr(client, "force_reconnect_stale_session"):
            client.force_reconnect_stale_session(
                f"print command unacknowledged after {timeout:.0f}s "
                f"(state still {pre_state}, gcode_file {current_gcode_file!r})"
            )
        return False

    @staticmethod
    async def _cleanup_sd_card_file(
        printer_ip: str,
        access_code: str,
        remote_path: str,
        printer_model: str | None,
    ):
        """Best-effort delete of uploaded file from printer SD card."""
        try:
            await delete_file_async(printer_ip, access_code, remote_path, printer_model=printer_model)
        except Exception:
            pass  # Best-effort - don't fail the error handler

    @staticmethod
    async def _mark_dispatch_archive_terminal(archive_id: int, status: str, error_message: str) -> None:
        """Flip a dispatch-time archive to a terminal state on error.

        ``archive_print`` commits the row before the upload/start block runs,
        so a later FTP or start-print failure can leave the archive stuck in
        "printing". This writes a terminal ``status`` + ``error_message`` +
        ``completed_at`` in a fresh session — only if the archive is still
        in "printing", so we don't clobber an on_print_complete transition
        that raced with us.
        """
        from datetime import datetime, timezone

        from backend.app.models.archive import PrintArchive

        try:
            async with async_session() as fdb:
                archive = await fdb.get(PrintArchive, archive_id)
                if archive is None or archive.status != "printing":
                    return
                archive.status = status
                archive.error_message = error_message
                archive.completed_at = datetime.now(timezone.utc)
                await fdb.commit()
        except Exception as cleanup_err:
            logger.warning(
                "Failed to mark dispatch archive %s as %s: %s",
                archive_id,
                status,
                cleanup_err,
            )

    @staticmethod
    def _resolve_plate_id(file_path: Path, requested_plate_id: int | None) -> int:
        if requested_plate_id is not None:
            return requested_plate_id

        plate_id = 1
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                for name in zf.namelist():
                    if name.startswith("Metadata/plate_") and name.endswith(".gcode"):
                        plate_str = name[15:-6]
                        plate_id = int(plate_str)
                        break
        except (ValueError, zipfile.BadZipFile, OSError):
            pass
        return plate_id

    @staticmethod
    def _is_sliced_file(filename: str) -> bool:
        lower = filename.lower()
        return lower.endswith(".gcode") or lower.endswith(".gcode.3mf")


background_dispatch = BackgroundDispatchService()
