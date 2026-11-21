"""The attempt's return to waiting and the plate backfill against lane holders (G03 B, A10 / L2(e); acceptance TA18).

(a) ``defer_claim`` -- the real deferral, under ``claim_lanes`` -- against a claim on the same queue, both orders, and
against an edit apply holding that queue's lane: the second waits on the lane and nothing deadlocks.
(b) PostgreSQL: the attach's plate backfill (``SKIP LOCKED``) against a writer holding one of two job rows of the same
archive: the backfill does not wait, fills the free row, leaves the held one to its writer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops
from backend.tests.integration.lane_pairs import (
    a_waiter_appears,
    bypass_the_process_lane,
    claim_holding_the_lane,
    go,
    held_inside,
    pending_rows,
)
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T0 = datetime(2026, 11, 20, 10, 0, 0)


async def an_attempt_and_a_waiting_job(db_session, printer_factory) -> tuple[int, int, int]:
    """Row R printing under the queue's claim (started T0), and the next job N pending behind it."""
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    running = PrintQueueItem(queue_id=queue_id, position=0, status="printing", started_at=T0)
    waiting = PrintQueueItem(queue_id=queue_id, position=1, status="pending")
    db_session.add_all([running, waiting])
    await db_session.flush()
    header = await db_session.get(PrinterQueue, queue_id)
    header.status, header.current_item_id = "printing", running.id
    await db_session.commit()
    return queue_id, running.id, waiting.id


async def defer(factory, item_id, queue_id):
    from backend.app.services.filament_deferred import defer_claim

    async with factory() as db:
        return await defer_claim(db, item_id=item_id, started_at=T0, queue_id=queue_id, reason="feed_settle_timeout")


async def test_a_deferral_meeting_a_claim_waits_on_the_lane(test_engine, db_session, printer_factory, monkeypatch):
    queue_id, running_id, waiting_id = await an_attempt_and_a_waiting_job(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    # The claim of the next job is only its lock shape here: it claims nothing (the header names R).
    holder = asyncio.create_task(claim_holding_the_lane(factory, queue_id, 0, reached, release))
    deferring = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        deferring = asyncio.create_task(defer(factory, running_id, queue_id))
        await a_waiter_appears(test_engine, factory, deferring, queue_id)
        release.set()
        await asyncio.wait_for(holder, 20)
        await asyncio.wait_for(deferring, 20)
    finally:
        release.set()
        await asyncio.gather(holder, *([deferring] if deferring else []), return_exceptions=True)
    async with factory() as db:
        row = await db.get(PrintQueueItem, running_id)
        assert (row.status, row.deferred_claim_token) == ("pending", T0)


async def test_a_claim_meeting_a_deferral_waits_on_the_lane_then_claims(
    test_engine, db_session, printer_factory, monkeypatch
):
    queue_id, running_id, waiting_id = await an_attempt_and_a_waiting_job(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    deferring = asyncio.create_task(defer(factory, running_id, queue_id))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, waiting_id, asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, queue_id)
        release.set()
        await asyncio.wait_for(deferring, 20)
        assert await asyncio.wait_for(claim, 20) == 1
    finally:
        release.set()
        await asyncio.gather(deferring, *([claim] if claim else []), return_exceptions=True)
    async with factory() as db:
        assert (await db.get(PrintQueueItem, running_id)).status == "pending"
        assert (await db.get(PrintQueueItem, waiting_id)).status == "printing"


async def test_a_deferral_meeting_an_edit_apply_waits_on_the_lane(
    test_engine, db_session, printer_factory, async_client, monkeypatch
):
    queue_id, running_id, waiting_id = await an_attempt_and_a_waiting_job(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    editing = asyncio.create_task(async_client.patch(f"/api/v1/queue/{waiting_id}", json={"auto_off_after": True}))
    deferring = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        deferring = asyncio.create_task(defer(factory, running_id, queue_id))
        await a_waiter_appears(test_engine, factory, deferring, queue_id)
        release.set()
        response = await asyncio.wait_for(editing, 20)
        await asyncio.wait_for(deferring, 20)
    finally:
        release.set()
        await asyncio.gather(editing, *([deferring] if deferring else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.get(PrintQueueItem, waiting_id)).auto_off_after is True
        assert (await db.get(PrintQueueItem, running_id)).status == "pending"


async def test_the_plate_backfill_skips_a_held_row_and_fills_the_free_one(
    test_engine, db_session, printer_factory, archive_factory
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("SKIP LOCKED is a PostgreSQL row-lock question; SQLite serialises on its writer")
    from backend.app.models.archive import PrintArchive
    from backend.app.services.archive import ArchiveService

    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    archive = await archive_factory(queue_id, status="printing", plate_index=3)
    held = PrintQueueItem(queue_id=queue_id, position=0, status="pending", archive_id=archive.id)
    free = PrintQueueItem(queue_id=queue_id, position=1, status="pending", archive_id=archive.id)
    db_session.add_all([held, free])
    await db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as writer:
        # A bulk PATCH inside its lanes, holding one of the two rows.
        await writer.scalar(
            select(PrintQueueItem.id).where(PrintQueueItem.id == held.id).with_for_update(key_share=True)
        )

        async def backfill():
            async with factory() as db:
                attached = await db.get(PrintArchive, archive.id)
                await ArchiveService(db)._backfill_job_plates(attached)
                await db.commit()

        # Completes while the row is held: SKIP LOCKED never waits.
        await asyncio.wait_for(backfill(), 10)
        async with factory() as monitor:
            assert not await monitor.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
                )
            )
        await writer.commit()
    async with factory() as db:
        assert (await db.get(PrintQueueItem, free.id)).plate_id == 3
        assert (await db.get(PrintQueueItem, held.id)).plate_id is None, "left to the writer that held it"
