import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.api.routes.slice_jobs import finish_assembly_search
from backend.app.services.slice_dispatch import slice_dispatch

API = "/api/v1"


@pytest_asyncio.fixture(autouse=True)
async def clean_jobs():
    await slice_dispatch.quiesce_for_restore(5)
    slice_dispatch.open = True
    yield
    await slice_dispatch.quiesce_for_restore(5)
    slice_dispatch.open = True


@pytest.mark.asyncio
async def test_finish_search_is_not_cancel_and_keeps_the_normal_result(async_client):
    async def work(job_id):
        slice_dispatch.set_progress(job_id, {"stage": "packing", "search_best_plates": 1, "search_best_copies": 8})
        while not slice_dispatch.get(job_id).finish_requested:
            await asyncio.sleep(0.01)
        return {"best": 8}

    job = await slice_dispatch.enqueue(kind="assembly", source_id=1, source_name="fixture", run=work)
    await asyncio.sleep(0)
    task = slice_dispatch._tasks[job.id]
    response = await async_client.post(f"{API}/slice-jobs/{job.id}/finish-search")
    assert response.status_code == 204
    await task
    state = (await async_client.get(f"{API}/slice-jobs/{job.id}")).json()
    assert state["status"] == "completed" and state["result"] == {"best": 8}
    assert state["finish_requested"] is True


@pytest.mark.asyncio
async def test_finish_refuses_missing_best_publication_and_unknown_job(async_client):
    ready = asyncio.Event()

    async def work(job_id):
        await ready.wait()
        return {}

    job = await slice_dispatch.enqueue(kind="assembly", source_id=1, source_name="fixture", run=work)
    await asyncio.sleep(0)
    task = slice_dispatch._tasks[job.id]
    try:
        for progress in ({"stage": "packing"}, {"stage": "publishing", "search_best_plates": 1}):
            slice_dispatch.set_progress(job.id, progress)
            assert (await async_client.post(f"{API}/slice-jobs/{job.id}/finish-search")).status_code == 409
            assert job.finish_requested is False
        assert (await async_client.post(f"{API}/slice-jobs/999999/finish-search")).status_code == 404
    finally:
        ready.set()
        await task


@pytest.mark.asyncio
async def test_finish_does_not_gain_another_owners_visibility():
    ready = asyncio.Event()

    async def work(job_id):
        await ready.wait()
        return {}

    job = await slice_dispatch.enqueue(
        kind="assembly",
        source_id=1,
        source_name="fixture",
        authority=SimpleNamespace(account_identity="owner-A"),
        run=work,
    )
    await asyncio.sleep(0)
    task = slice_dispatch._tasks[job.id]
    slice_dispatch.set_progress(job.id, {"stage": "packing", "search_best_plates": 1})
    try:
        with pytest.raises(HTTPException) as refused:
            await finish_assembly_search(job.id, (SimpleNamespace(account_identity="owner-B"), False), None)
        assert refused.value.status_code == 404 and job.finish_requested is False
    finally:
        ready.set()
        await task
