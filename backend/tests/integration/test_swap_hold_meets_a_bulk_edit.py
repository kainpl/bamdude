"""The completion's swap hold meets a bulk edit of the printer's next waiting job, both orders (G03 B, A6 / A21;
acceptance TA26 "the completion's swap hold vs a bulk PATCH holding the next waiting row").

Before the change the hold took the header, then the waiting row, while the bulk edit held its rows and recounted the
header -- a cycle. Both are lane writers now: whichever holds the lane, the other waits on it, and both commit -- the
queue paused with the reason on the waiting job, the edit applied.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops
from backend.tests.integration.lane_pairs import a_waiter_appears, bypass_the_process_lane, held_inside, pending_rows
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_finished_swap_run(db_session, printer_factory, archive_factory) -> tuple[int, int, int, int]:
    queue_id, (waiting_id,) = await pending_rows(db_session, printer_factory, count=1)
    archive = await archive_factory(queue_id, status="completed")
    run = PrintQueueItem(queue_id=queue_id, position=0, status="completed", archive_id=archive.id)
    db_session.add(run)
    await db_session.flush()
    header = await db_session.get(PrinterQueue, queue_id)
    header.status, header.current_item_id = "printing", run.id
    await db_session.commit()
    return queue_id, archive.id, run.id, waiting_id


def swap_hold(queue_id, archive_id, run_id):
    from backend.app import main

    return main._pause_after_swap_failure(queue_id, archive_id, run_id, "Table swap failed", may_hold=lambda: True)


def bulk_edit(client, waiting_id):
    return client.patch("/api/v1/queue/bulk", json={"item_ids": [waiting_id], "auto_off_after": True})


async def outcome(factory, queue_id, waiting_id):
    async with factory() as db:
        header = await db.get(PrinterQueue, queue_id)
        waiting = await db.get(PrintQueueItem, waiting_id)
    assert header.status == "paused" or header.is_paused, (header.status, header.is_paused)
    assert waiting.auto_off_after is True, "the edit applied"
    assert waiting.waiting_reason == "Table swap failed", "the hold's reason on the waiting job"


@pytest.fixture
def factory(test_engine, monkeypatch):
    from backend.app import main

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", sessions)
    return sessions


async def test_a_bulk_edit_meeting_the_swap_hold_waits_on_the_lane(
    test_engine, factory, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    queue_id, archive_id, run_id, waiting_id = await a_finished_swap_run(db_session, printer_factory, archive_factory)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    holding = asyncio.create_task(swap_hold(queue_id, archive_id, run_id))
    editing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        editing = asyncio.create_task(bulk_edit(async_client, waiting_id))
        await a_waiter_appears(test_engine, factory, editing, queue_id)
        release.set()
        await asyncio.wait_for(holding, 20)
        response = await asyncio.wait_for(editing, 20)
    finally:
        release.set()
        await asyncio.gather(holding, *([editing] if editing else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, queue_id, waiting_id)


async def test_the_swap_hold_meeting_a_bulk_edit_waits_on_the_lane(
    test_engine, factory, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    queue_id, archive_id, run_id, waiting_id = await a_finished_swap_run(db_session, printer_factory, archive_factory)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    editing = asyncio.create_task(bulk_edit(async_client, waiting_id))
    holding = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        holding = asyncio.create_task(swap_hold(queue_id, archive_id, run_id))
        await a_waiter_appears(test_engine, factory, holding, queue_id)
        release.set()
        response = await asyncio.wait_for(editing, 20)
        await asyncio.wait_for(holding, 20)
    finally:
        release.set()
        await asyncio.gather(editing, *([holding] if holding else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, queue_id, waiting_id)
