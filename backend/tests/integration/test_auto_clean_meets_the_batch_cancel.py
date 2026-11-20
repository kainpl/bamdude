"""The completion's auto-clean of a finished row meets the batch cancel of its AutoQueue companion, both orders (G03 B,
A20 / A21; acceptance TA26 "auto-clean vs batch cancel of its companion: no cycle").

The auto-clean takes the row's lane and archive, the row, then the companion router row through the detach's prelock.
The batch cancel takes its footprint's lanes, then its router rows ascending. Whichever is held first, the other only
waits -- no cycle, no ``40P01`` -- and both finish; the router row survives (it was routed), the finished row is
cleaned or kept by the clean-up's own rule.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_counters, queue_ops
from backend.tests.integration.lane_pairs import bypass_the_process_lane, held_after, held_inside, pending_rows
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_finished_companion(db_session, printer_factory, archive_factory) -> tuple[int, int, int]:
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    archive = await archive_factory(queue_id, status="completed")
    router = AutoQueueItem(status="assigned", position=1, batch_id="b-1")
    db_session.add(router)
    await db_session.flush()
    finished = PrintQueueItem(
        queue_id=queue_id, position=0, status="completed", archive_id=archive.id, source_auto_item_id=router.id
    )
    db_session.add(finished)
    await db_session.flush()
    router.assigned_to_item_id = finished.id
    await db_session.commit()
    return queue_id, router.id, finished.id


async def auto_clean(factory, item_id):
    from backend.app import main

    async with factory() as db:
        item = await db.get(PrintQueueItem, item_id)
        return await main._auto_clean_completed_item(db, item, queue_status="completed", plate_auto_cleared=False)


async def batch_cancel():
    from backend.app.api.routes.auto_queue import cancel_auto_queue_batch
    from backend.app.core import database

    async with database.async_session() as db:
        return await cancel_auto_queue_batch("b-1", db=db, _=None)


async def both_finish(first, second_factory, reached, release):
    second = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        second = asyncio.create_task(second_factory())
        await asyncio.sleep(0.5)  # the second runs into whatever the first holds
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, second), 30)
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


async def outcome(factory, router_id, finished_id):
    async with factory() as db:
        router = await db.get(AutoQueueItem, router_id)
        finished = await db.get(PrintQueueItem, finished_id)
    assert router is not None and router.status == "assigned", "a routed row is never deleted by the batch cancel"
    assert finished is None or finished.status == "completed"


async def test_the_batch_cancel_meeting_the_auto_clean_holding_its_lane(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch
):
    queue_id, router_id, finished_id = await a_finished_companion(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    cleaning = asyncio.create_task(auto_clean(factory, finished_id))
    await both_finish(cleaning, batch_cancel, reached, release)
    await outcome(factory, router_id, finished_id)


async def test_the_auto_clean_meeting_the_batch_cancel_holding_the_router_row(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch
):
    queue_id, router_id, finished_id = await a_finished_companion(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_counters, "lock_auto_rows")
    cancelling = asyncio.create_task(batch_cancel())
    await both_finish(cancelling, lambda: auto_clean(factory, finished_id), reached, release)
    await outcome(factory, router_id, finished_id)
