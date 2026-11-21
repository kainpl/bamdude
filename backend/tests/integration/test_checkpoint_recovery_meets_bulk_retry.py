"""The recovery of an accepted-but-interrupted terminal meets bulk Retry of an older failed row, both orders (G03 B,
A18; acceptance TA22 for ``_recover_accepted_terminal_checkpoint``).

The recovery closes exactly the archived attempt and pauses its queue, under the printer's lanes with the archive's
guard -- a lane writer. Bulk Retry of the queue's older failed row holds the same lane and that row. Whichever goes
first, the other waits on the lane (never on the header or a row) and both commit.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_counters, queue_ops
from backend.tests.integration.lane_pairs import a_waiter_appears, bypass_the_process_lane, held_after, held_inside
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_lane_writers_concurrency import (  # noqa: F401 -- fixture
    a_run_and_an_older_failure,
    no_plate_gate,
    retry,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def an_accepted_terminal(db_session, printer_factory, archive_factory):
    from backend.app import main

    queue_id, archive_id, running_id, failed_id, actor = await a_run_and_an_older_failure(
        db_session, printer_factory, archive_factory
    )
    running = await db_session.get(PrintQueueItem, running_id)
    marker = main._terminal_acceptance_payload(running, "completed")
    await db_session.execute(
        update(PrintArchive)
        .where(PrintArchive.id == archive_id)
        .values(extra_data={main._TERMINAL_ACCEPTANCE_KEY: marker})
    )
    await db_session.commit()
    return queue_id, archive_id, running_id, failed_id, actor


async def recover(factory, archive_id):
    from backend.app.services.print_reconciliation import _recover_accepted_terminal_checkpoint

    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        return await _recover_accepted_terminal_checkpoint(db, archive)


async def outcome(factory, running_id, failed_id):
    async with factory() as db:
        running = await db.get(PrintQueueItem, running_id)
        failed = await db.get(PrintQueueItem, failed_id)
    assert running is None or running.status != "printing", "the accepted attempt is closed"
    assert failed.status == "pending", "the retry re-armed the older failure"


async def test_a_bulk_retry_meeting_the_checkpoint_recovery_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch, no_plate_gate
):
    queue_id, archive_id, running_id, failed_id, actor = await an_accepted_terminal(
        db_session, printer_factory, archive_factory
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_inside(monkeypatch, queue_ops, "claim_lanes")
    recovering = asyncio.create_task(recover(factory, archive_id))
    retrying = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        retrying = asyncio.create_task(retry(factory, queue_id, failed_id, actor))
        await a_waiter_appears(test_engine, factory, retrying, queue_id)
        release.set()
        assert await asyncio.wait_for(recovering, 20) == archive_id
        assert (await asyncio.wait_for(retrying, 20))["retried"] == [failed_id]
    finally:
        release.set()
        await asyncio.gather(recovering, *([retrying] if retrying else []), return_exceptions=True)
    await outcome(factory, running_id, failed_id)


async def test_the_checkpoint_recovery_meeting_a_bulk_retry_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch, no_plate_gate
):
    queue_id, archive_id, running_id, failed_id, actor = await an_accepted_terminal(
        db_session, printer_factory, archive_factory
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_counters, "update_queue_counters")
    retrying = asyncio.create_task(retry(factory, queue_id, failed_id, actor))
    recovering = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        recovering = asyncio.create_task(recover(factory, archive_id))
        # SQLite: the recovery's first commit already waits for the writer the retry holds.
        await a_waiter_appears(test_engine, factory, recovering, lane=queue_id)
        release.set()
        assert (await asyncio.wait_for(retrying, 20))["retried"] == [failed_id]
        assert await asyncio.wait_for(recovering, 20) == archive_id
    finally:
        release.set()
        await asyncio.gather(retrying, *([recovering] if recovering else []), return_exceptions=True)
    await outcome(factory, running_id, failed_id)
