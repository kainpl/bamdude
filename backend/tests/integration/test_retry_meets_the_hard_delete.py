"""Retry restoring a job's link to its original meets the original's hard delete, both orders (G03 B, A19 / A27;
acceptance TA37 (g)).

The job was captured from archive A (its provenance stamped with A's incarnation) and its attempt re-pointed it to X.
Retry takes A ``FOR KEY SHARE`` before the job row (L5, L6(d)) and writes the link back to A only while A is the
incarnation the capture stamped. Forward: Retry holds A; the hard delete of A waits (its DELETE meets the share),
then either refuses -- a row naming A appeared in a lane outside its footprint, ``queue_selection_changed`` -- or
takes that row as a dependent; nothing deadlocks. Reverse: the delete holds A under its plan; Retry waits on A, then
finds it gone and writes NULL -- the job prints its own bytes.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_source_link
from backend.tests.integration.lane_pairs import a_waiter_appears, bypass_the_process_lane, held_after
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_source_link_doors import a_finished_job

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# The delete may wait on A's row (its DELETE against Retry's share) or on a guard; Retry on A's row.
ANY_WAIT = ("transactionid", "tuple", "advisory")


async def hard_delete(factory, archive_id):
    from backend.app.services.archive import ArchiveService

    async with factory() as db:
        return await ArchiveService(db).delete_archive(archive_id)


async def test_a_hard_delete_meeting_a_retry_that_holds_the_original_waits_then_refuses_or_takes_the_row(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    _printer, queue_id, original_id, _x, item_id = await a_finished_job(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_source_link, "lock_parent")
    retrying = asyncio.create_task(async_client.post(f"/api/v1/queue/{item_id}/retry"))
    deleting = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        deleting = asyncio.create_task(hard_delete(factory, original_id))
        await a_waiter_appears(test_engine, factory, deleting, on=ANY_WAIT)
        release.set()
        response = await asyncio.wait_for(retrying, 20)
        try:
            deleted = await asyncio.wait_for(deleting, 20)
        except HTTPException as refused:
            deleted = refused
    finally:
        release.set()
        await asyncio.gather(retrying, *([deleting] if deleting else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        job = await db.get(PrintQueueItem, item_id)
        original = await db.get(PrintArchive, original_id)
    if isinstance(deleted, HTTPException):
        # The row Retry linked to A is in a lane the delete did not plan: refused whole, A kept, the link kept.
        assert deleted.status_code == 409 and deleted.detail["code"] == "queue_selection_changed", deleted.detail
        assert original is not None and job.archive_id == original_id
    else:
        # The delete planned the row as A's dependent: A gone, and no link to it left behind.
        assert original is None and job.archive_id is None, (deleted, job.archive_id)


async def test_a_retry_meeting_the_hard_delete_of_its_original_waits_then_writes_no_link(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    from backend.app.services import archive_delete_scope

    _printer, queue_id, original_id, _x, item_id = await a_finished_job(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, archive_delete_scope.ArchiveDeletePlan, "recheck")
    deleting = asyncio.create_task(hard_delete(factory, original_id))
    retrying = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        retrying = asyncio.create_task(async_client.post(f"/api/v1/queue/{item_id}/retry"))
        await a_waiter_appears(test_engine, factory, retrying, on=ANY_WAIT)
        release.set()
        assert await asyncio.wait_for(deleting, 20)
        response = await asyncio.wait_for(retrying, 20)
    finally:
        release.set()
        await asyncio.gather(deleting, *([retrying] if retrying else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert await db.get(PrintArchive, original_id) is None
        job = await db.get(PrintQueueItem, item_id)
    assert (job.status, job.archive_id) == ("pending", None), "no link to a gone original; the job keeps its bytes"
