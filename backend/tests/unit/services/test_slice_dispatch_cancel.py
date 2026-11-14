"""Stopping a slice-dispatch job: the route's cancel, and the drain a restore asks for."""

import asyncio

import pytest

from backend.app.services.slice_dispatch import SliceDispatchService


async def _job(service, run, kind="assembly"):
    return await service.enqueue(kind=kind, source_id=1, source_name="x", run=run)


@pytest.mark.asyncio
async def test_cancel_ends_a_running_job_as_failed_499():
    service = SliceDispatchService()
    started, released = asyncio.Event(), []

    async def run(job_id):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            released.append(job_id)

    job = await _job(service, run)
    await started.wait()
    assert service.cancel(job.id) is True
    await asyncio.sleep(0.05)
    assert (job.status, job.error_status, job.error_detail) == ("failed", 499, "Canceled")
    assert released == [job.id] and job.completed_at is not None and job.id not in service._tasks
    assert service.cancel(job.id) is False, "nothing is left to stop"


@pytest.mark.asyncio
async def test_cancel_before_the_first_step_leaves_no_task_behind():
    service = SliceDispatchService()
    ran = []

    async def run(job_id):
        ran.append(job_id)
        return {}

    job = await _job(service, run)
    assert service.cancel(job.id) is True  # the task has not had the loop yet
    await asyncio.sleep(0.05)
    assert not ran and job.status == "failed" and job.error_status == 499 and job.id not in service._tasks


@pytest.mark.asyncio
async def test_cancel_of_a_finished_or_unknown_job_is_false():
    service = SliceDispatchService()

    async def run(job_id):
        return {"ok": True}

    job = await _job(service, run)
    await asyncio.sleep(0.05)
    assert job.status == "completed" and service.cancel(job.id) is False
    assert job.status == "completed" and job.result == {"ok": True}
    assert service.cancel(999) is False


@pytest.mark.asyncio
async def test_a_restore_drains_an_assembly_job_and_it_is_not_left_running():
    service = SliceDispatchService()
    started, released = asyncio.Event(), []

    async def run(job_id):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            released.append(job_id)

    job = await _job(service, run)
    await started.wait()
    assert await service.quiesce_for_restore(5) is True
    assert released == [job.id] and (job.status, job.error_status) == ("failed", 499)
    with pytest.raises(Exception, match="unavailable during restore"):
        await _job(service, run)
