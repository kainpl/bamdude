"""Printer deletion meets the end of an attempt on its last job, both orders (G03 B, A10 / A21; acceptance TA26
"printer deletion vs an attempt end on its last job: no cycle").

Both are lane writers of the printer's queue: the deletion's last step takes the lanes with the printer row as its
early row, then the headers ``FOR UPDATE`` and the job rows; the deferral takes the claim queue's lane with its
archives, then the job row. Whichever holds the lane, the other waits on it -- no cycle -- and both finish: the
deletion removes the job with the queue; a deferral after it finds no row of its own.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import queue_ops
from backend.tests.integration.lane_pairs import a_waiter_appears, bypass_the_process_lane, held_inside
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_deferral_and_backfill_meet_lane_holders import an_attempt_and_a_waiting_job, defer

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def gone(factory, printer_id, *item_ids):
    async with factory() as db:
        assert await db.get(Printer, printer_id) is None
        for item_id in item_ids:
            assert await db.get(PrintQueueItem, item_id) is None


async def test_an_attempt_end_meeting_the_printer_deletion_waits_on_the_lane(
    test_engine, db_session, printer_factory, async_client, monkeypatch
):
    queue_id, running_id, waiting_id = await an_attempt_and_a_waiting_job(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    deleting = asyncio.create_task(async_client.delete(f"/api/v1/printers/{queue_id}?delete_archives=false"))
    deferring = None
    try:
        await asyncio.wait_for(reached.wait(), 15)
        deferring = asyncio.create_task(defer(factory, running_id, queue_id))
        await a_waiter_appears(test_engine, factory, deferring, queue_id)
        release.set()
        response = await asyncio.wait_for(deleting, 30)
        result = await asyncio.wait_for(deferring, 30)
    finally:
        release.set()
        await asyncio.gather(deleting, *([deferring] if deferring else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    assert not result.owned, "the job went with the queue"
    await gone(factory, queue_id, running_id, waiting_id)


async def test_the_printer_deletion_meeting_an_attempt_end_waits_on_the_lane(
    test_engine, db_session, printer_factory, async_client, monkeypatch
):
    queue_id, running_id, waiting_id = await an_attempt_and_a_waiting_job(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    deferring = asyncio.create_task(defer(factory, running_id, queue_id))
    deleting = None
    try:
        await asyncio.wait_for(reached.wait(), 15)
        deleting = asyncio.create_task(async_client.delete(f"/api/v1/printers/{queue_id}?delete_archives=false"))
        await asyncio.sleep(0.5)
        assert not deleting.done(), "the deletion waits for the attempt end's lane"
        release.set()
        assert (await asyncio.wait_for(deferring, 30)).owned
        response = await asyncio.wait_for(deleting, 30)
    finally:
        release.set()
        await asyncio.gather(deferring, *([deleting] if deleting else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await gone(factory, queue_id, running_id, waiting_id)
