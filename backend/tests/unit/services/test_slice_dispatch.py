"""Tests for SliceDispatchService — Phase 1 of the 0.5.x slicer cycle.

In-memory job lifecycle, retention, error propagation, progress wiring.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.app.services.slice_dispatch import (
    _RETENTION_SECONDS,
    SliceDispatchService,
    _SliceJobError,
    http_exception_to_job_error,
)


class TestEnqueueRunComplete:
    @pytest.mark.asyncio
    async def test_happy_path_completes_with_result(self):
        svc = SliceDispatchService()

        async def runner(job_id: int) -> dict:
            return {"library_file_id": 42, "name": "ok.gcode.3mf"}

        job = await svc.enqueue(kind="library_file", source_id=1, source_name="src.3mf", run=runner)
        # Run is fire-and-forget; wait for the spawned task to finish.
        await asyncio.wait_for(svc._tasks[job.id], timeout=2.0) if job.id in svc._tasks else None
        # Poll because completion happens on the event loop.
        for _ in range(50):
            j = svc.get(job.id)
            if j and j.status == "completed":
                break
            await asyncio.sleep(0.02)
        j = svc.get(job.id)
        assert j is not None
        assert j.status == "completed"
        assert j.result == {"library_file_id": 42, "name": "ok.gcode.3mf"}
        assert j.started_at is not None
        assert j.completed_at is not None

    @pytest.mark.asyncio
    async def test_set_progress_threads_through_to_job(self):
        svc = SliceDispatchService()
        completion = asyncio.Event()

        async def runner(job_id: int) -> dict:
            svc.set_progress(job_id, {"stage": "slicing", "total_percent": 25})
            await completion.wait()
            return {}

        job = await svc.enqueue(kind="library_file", source_id=1, source_name="x.3mf", run=runner)
        for _ in range(50):
            j = svc.get(job.id)
            if j and j.progress is not None:
                break
            await asyncio.sleep(0.02)
        j = svc.get(job.id)
        assert j.progress == {"stage": "slicing", "total_percent": 25}
        completion.set()
        await asyncio.wait_for(svc._tasks.get(job.id, asyncio.sleep(0)), timeout=2.0)


class TestEnqueueErrorPaths:
    @pytest.mark.asyncio
    async def test_slice_job_error_propagates_status_and_detail(self):
        svc = SliceDispatchService()

        async def runner(job_id: int) -> dict:
            raise _SliceJobError(400, "Invalid printer preset id")

        job = await svc.enqueue(kind="library_file", source_id=1, source_name="x", run=runner)
        for _ in range(50):
            j = svc.get(job.id)
            if j and j.status == "failed":
                break
            await asyncio.sleep(0.02)
        j = svc.get(job.id)
        assert j.status == "failed"
        assert j.error_status == 400
        assert "Invalid printer preset" in j.error_detail

    @pytest.mark.asyncio
    async def test_unexpected_exception_becomes_500(self):
        svc = SliceDispatchService()

        async def runner(job_id: int) -> dict:
            raise RuntimeError("kaboom")

        job = await svc.enqueue(kind="library_file", source_id=1, source_name="x", run=runner)
        for _ in range(50):
            j = svc.get(job.id)
            if j and j.status == "failed":
                break
            await asyncio.sleep(0.02)
        j = svc.get(job.id)
        assert j.status == "failed"
        assert j.error_status == 500
        assert "kaboom" in j.error_detail


class TestRetentionSweep:
    @pytest.mark.asyncio
    async def test_old_completed_jobs_swept_on_next_enqueue(self):
        svc = SliceDispatchService()

        # Inject a stale completed job manually.
        async def quick(job_id: int) -> dict:
            return {}

        first = await svc.enqueue(kind="library_file", source_id=1, source_name="a", run=quick)
        for _ in range(50):
            if svc.get(first.id) and svc.get(first.id).status == "completed":
                break
            await asyncio.sleep(0.02)
        # Backdate completion past the retention window.
        svc._jobs[first.id].completed_at = datetime.now(timezone.utc) - timedelta(seconds=_RETENTION_SECONDS + 60)

        # Enqueue a fresh job — sweep should drop the stale one.
        second = await svc.enqueue(kind="library_file", source_id=2, source_name="b", run=quick)
        assert first.id not in svc._jobs
        assert second.id in svc._jobs

    @pytest.mark.asyncio
    async def test_recent_completed_jobs_kept(self):
        svc = SliceDispatchService()

        async def quick(job_id: int) -> dict:
            return {}

        first = await svc.enqueue(kind="library_file", source_id=1, source_name="a", run=quick)
        for _ in range(50):
            if svc.get(first.id) and svc.get(first.id).status == "completed":
                break
            await asyncio.sleep(0.02)
        second = await svc.enqueue(kind="library_file", source_id=2, source_name="b", run=quick)
        # Both still around — retention window not breached.
        assert first.id in svc._jobs
        assert second.id in svc._jobs


class TestHttpExceptionConversion:
    def test_http_exception_to_job_error_preserves_status_and_detail(self):
        exc = HTTPException(status_code=403, detail="Cloud presets require cloud:auth")
        err = http_exception_to_job_error(exc)
        assert isinstance(err, _SliceJobError)
        assert err.status_code == 403
        assert "cloud:auth" in err.detail


class TestUnknownIdsTolerated:
    @pytest.mark.asyncio
    async def test_set_progress_unknown_id_is_silent(self):
        svc = SliceDispatchService()
        # Must not raise — progress tracker may fire after retention sweep.
        svc.set_progress(99999, {"stage": "stale"})

    def test_get_unknown_returns_none(self):
        svc = SliceDispatchService()
        assert svc.get(99999) is None


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_inflight_tasks(self):
        svc = SliceDispatchService()
        block = asyncio.Event()

        async def long_running(job_id: int) -> dict:
            await block.wait()
            return {}

        job = await svc.enqueue(kind="library_file", source_id=1, source_name="x", run=long_running)
        # Job is running; shutdown cancels it.
        await svc.shutdown()
        # After shutdown, the task should be done (cancelled).
        for _ in range(50):
            if job.id not in svc._tasks:
                break
            await asyncio.sleep(0.02)
        # Job ends up either failed or cancelled — depends on cancel race.
        # The contract is just: no leaked tasks.
        assert job.id not in svc._tasks
