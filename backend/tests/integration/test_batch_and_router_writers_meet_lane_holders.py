"""Batch writers, the AutoQueue reorder and a verdict meet lane and row holders, both orders (G03 B; acceptance
TA17 for ungroup / batch skip / a batch's manual start, TA25 for the reorder against the waiting batch, TA24 for the
source-failure verdict against the archive trash). Helpers: ``lane_pairs``; the claim is its lock shape.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_counters, queue_ops
from backend.tests.integration.lane_pairs import (
    ROW_WAIT,
    a_waiter_appears,
    bypass_the_process_lane,
    claim_holding_the_lane,
    go,
    held_after,
    pending_rows,
)
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_lane_writers_concurrency import a_job_of_an_archive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ---------------------------------------------------------------- TA17: the batch writers against a claim of a member


async def a_batch(db_session, printer_factory) -> tuple[int, list[int]]:
    queue_id, ids = await pending_rows(db_session, printer_factory)
    await db_session.execute(update(PrintQueueItem).where(PrintQueueItem.id.in_(ids)).values(batch_id="b-1"))
    await db_session.commit()
    return queue_id, ids


# batch writer: (its request, the queue_ops function it is held inside / after, what the claimed member and the other
# member are once the writer went second, then once it went first -- the claim then claims the first member or not)
BATCH_WRITERS = {
    "ungroup": (
        lambda c, q, ids: c.post("/api/v1/queue/batch/b-1/ungroup", params={"queue_id": q}),
        "lane_session",
        (("printing", None), ("pending", None)),
        (("printing", None), ("pending", None)),
    ),
    "batch_skip": (
        lambda c, q, ids: c.post("/api/v1/queue/batch/b-1/skip", params={"queue_id": q}),
        "lane_session",
        (("printing", "b-1"), ("skipped", "b-1")),
        (("skipped", "b-1"), ("skipped", "b-1")),
    ),
    "batch_manual_start": (
        lambda c, q, ids: c.patch(f"/api/v1/queue/{ids[1]}/manual-start"),
        "lane_session",
        (("printing", "b-1"), ("pending", "b-1")),
        (("pending", "b-1"), ("pending", "b-1")),
    ),
}


async def members(factory, ids):
    async with factory() as db:
        rows = {row.id: row for row in await db.scalars(select(PrintQueueItem).where(PrintQueueItem.id.in_(ids)))}
    return tuple((rows[i].status, rows[i].batch_id) for i in ids)


@pytest.mark.parametrize("name", sorted(BATCH_WRITERS))
async def test_a_batch_writer_meeting_a_claim_of_a_member_waits_on_the_lane(
    name, test_engine, db_session, printer_factory, async_client, monkeypatch
):
    request, _held, second, _first = BATCH_WRITERS[name]
    queue_id, ids = await a_batch(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, ids[0], reached, release))
    writing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        writing = asyncio.create_task(request(async_client, queue_id, ids))
        await a_waiter_appears(test_engine, factory, writing, queue_id)
        release.set()
        assert await asyncio.wait_for(claim, 20) == 1
        response = await asyncio.wait_for(writing, 20)
    finally:
        release.set()
        await asyncio.gather(claim, *([writing] if writing else []), return_exceptions=True)
    assert response.status_code in (200, 409), response.text
    assert await members(factory, ids) == second, "the writer touched only what was still its to touch"


@pytest.mark.parametrize("name", sorted(BATCH_WRITERS))
async def test_a_claim_meeting_a_batch_writer_waits_on_the_lane(
    name, test_engine, db_session, printer_factory, async_client, monkeypatch
):
    from contextlib import asynccontextmanager

    request, held, _second, first = BATCH_WRITERS[name]
    queue_id, ids = await a_batch(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(queue_ops, held)

    @asynccontextmanager
    async def held_lane(*args, **kwargs):
        async with real(*args, **kwargs) as value:
            if not reached.is_set():
                reached.set()
                await asyncio.wait_for(release.wait(), 20)
            yield value

    monkeypatch.setattr(queue_ops, held, held_lane)
    writing = asyncio.create_task(request(async_client, queue_id, ids))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, ids[0], asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, queue_id)
        release.set()
        response = await asyncio.wait_for(writing, 20)
        await asyncio.wait_for(claim, 20)
    finally:
        release.set()
        await asyncio.gather(writing, *([claim] if claim else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    if name == "ungroup":
        # The members left the batch; the claim then took the first one.
        assert await members(factory, ids) == (("printing", None), ("pending", None))
    else:
        assert await members(factory, ids) == first


# ---------------------------------------------------------------- TA25: the AutoQueue reorder against the waiting batch


@pytest.mark.parametrize("reorder_first", [False, True], ids=["batch-first", "reorder-first"])
async def test_the_auto_queue_reorder_and_the_waiting_batch_lock_their_rows_ascending(
    reorder_first, test_engine, db_session, async_client, monkeypatch
):
    """``POST /auto-queue/reorder`` naming [2, 1] and the pass's waiting batch writing [1, 2]: both lock the rows
    ascending, so whichever goes first the other waits on a row -- never a cycle -- and both commit."""
    from backend.app.services.auto_queue_eligibility import router_row_view, write_router_rows

    rows = [AutoQueueItem(status="pending", position=i) for i in (1, 2)]
    db_session.add_all(rows)
    await db_session.commit()
    for row in rows:
        await db_session.refresh(row)
    low, high = sorted(row.id for row in rows)
    views = {row.id: router_row_view(row) for row in rows}
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    reached, release = asyncio.Event(), asyncio.Event()
    real = queue_counters.lock_auto_rows

    async def held_by_the_first(db, wanted):
        locked = await real(db, wanted)
        if not reached.is_set():
            reached.set()
            await asyncio.wait_for(release.wait(), 20)
        return locked

    from backend.app.api.routes import auto_queue as auto_queue_routes

    monkeypatch.setattr(queue_counters, "lock_auto_rows", held_by_the_first)
    monkeypatch.setattr(auto_queue_routes, "lock_auto_rows", held_by_the_first)  # bound at the route's import

    async def waiting_batch():
        async with factory() as db:
            return await write_router_rows(db, {i: (views[i], {"waiting_reason": "no printer"}) for i in (low, high)})

    def reorder():
        return async_client.post(
            "/api/v1/auto-queue/reorder", json={"items": [{"id": high, "position": 1}, {"id": low, "position": 2}]}
        )

    first = asyncio.create_task(reorder() if reorder_first else waiting_batch())
    second = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        second = asyncio.create_task(waiting_batch() if reorder_first else reorder())
        await a_waiter_appears(test_engine, factory, second, on=ROW_WAIT)
        release.set()
        outcomes = [await asyncio.wait_for(first, 20), await asyncio.wait_for(second, 20)]
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    response = outcomes[0] if reorder_first else outcomes[1]
    assert response.status_code == 200, response.text
    async with factory() as db:
        positions = dict((await db.execute(select(AutoQueueItem.id, AutoQueueItem.position))).all())
    assert positions == {high: 1, low: 2}


# ---------------------------------------------------------------- TA24: the archive trash against the source-failure verdict


async def test_an_archive_trash_meeting_the_source_failure_verdict_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    """TA24, V2: the pass fails a job whose archive source cannot be read, stopped under its lane; the trash of that
    archive waits on the lane, then finds the job already failed -- nothing to cancel -- and trashes the archive."""
    from backend.app.models.archive import PrintArchive
    from backend.app.services.print_scheduler import PrintScheduler
    from backend.app.services.queue_row_basis import row_basis

    queue_id, archive_id, item_id = await a_job_of_an_archive(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", factory)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, PrintScheduler, "_prove_verdict")

    async def source_failure():
        async with factory() as db:
            item = await db.get(PrintQueueItem, item_id)
            return await PrintScheduler()._fail_source_item(db, item, "source_unreadable", basis=row_basis(item))

    judging = asyncio.create_task(source_failure())
    trash = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        trash = asyncio.create_task(async_client.delete(f"/api/v1/archives/{archive_id}"))
        await a_waiter_appears(test_engine, factory, trash, queue_id)
        release.set()
        assert (await asyncio.wait_for(judging, 20)).written
        response = await asyncio.wait_for(trash, 20)
    finally:
        release.set()
        await asyncio.gather(judging, *([trash] if trash else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.get(PrintQueueItem, item_id)).status == "failed"
        assert (await db.get(PrintArchive, archive_id)).deleted_at is not None
