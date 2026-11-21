"""A head writer and a verdict of the pass meet on the lane, both orders (G03 B, A8 / A15; acceptance TA19).

A = a real verdict of the pass -- V1 (the previous-success gate's skip), V2 (a source failure), V3 (a failure before
the claim), V4 (a full timelapse medium's pause) -- stopped under its lane after its head proof. B = a real head
writer acting on a row ahead of (or behind) the judged head: ``/start``, the manual-start toggle, Unskip, a single
PATCH of ``scheduled_time``, the single reorder, Retry. Forward: B waits on the lane, A commits its verdict, then B
applies. Reverse: B is stopped inside its lane, A waits on the lane, then its head query returns the row B made the
head -- ``not_first_runnable``, nothing written (Retry re-appends its row at the end, so the judged row is still the
head and the verdict is written). Every verdict meets ``/start``; V1 meets every writer. And a row ahead that becomes
due while A waits for the lane is seen by A's ``now`` read after the grant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.services import queue_ops
from backend.app.services.print_scheduler import PrintScheduler, first_runnable_query
from backend.tests.integration.lane_pairs import (
    a_waiter_appears,
    bypass_the_process_lane,
    claim_holding_the_lane,
    held_after,
    held_inside,
    pending_rows,
)
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def factory(test_engine, monkeypatch):
    from backend.app.services.notification_service import notification_service

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    # The verdict's own session comes from the scheduler module's name, bound at import.
    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", sessions)
    monkeypatch.setattr(notification_service, "on_queue_job_skipped", AsyncMock())
    # V4 asks whether the medium is still full: it is, for the whole test.
    monkeypatch.setattr("backend.app.services.print_scheduler._timelapse_storage_full", lambda _printer_id: True)
    return sessions


# The row the writer acts on, and where it sits: ``front`` ahead of the judged head, ``behind`` after it.
FRONTS = {
    "staged": ({"manual_start": True}, 1),
    "skipped": ({"status": "skipped"}, 1),
    "scheduled": ({"scheduled_time": datetime(2099, 1, 1)}, 1),
    "behind": ({}, 3),
    "failed": ({"status": "failed"}, 1),
}


async def a_gated_head(db_session, printer_factory, front="staged", **ahead) -> tuple[int, int, int]:
    """Queue Q: an unacknowledged failure, a row the writer acts on, and the judged head that runs only after a
    successful previous print. A captured source on the writer's row (Unskip and Retry reuse it)."""
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    blob = QueueSource(sha256="e" * 64, size_bytes=1, relative_path="objects/ee/job.3mf", format="3mf", state="ready")
    db_session.add(blob)
    await db_session.flush()
    failed = PrintQueueItem(
        queue_id=queue_id, position=0, status="failed", completed_at=utcnow() - timedelta(minutes=10)
    )
    columns, position = FRONTS[front]
    columns = {"status": "pending", **columns, **ahead}
    if columns["status"] in ("skipped", "failed"):
        columns["completed_at"] = utcnow() - timedelta(minutes=1)
    row = PrintQueueItem(queue_id=queue_id, position=position, queue_source_id=blob.id, **columns)
    judged = PrintQueueItem(queue_id=queue_id, position=2, status="pending", require_previous_success=True)
    db_session.add_all([failed, row, judged])
    await db_session.commit()
    return queue_id, row.id, judged.id


async def verdict(factory, name, judged_id):
    from backend.app.services.queue_row_basis import row_basis

    scheduler = PrintScheduler()
    async with factory() as db:
        # As the pass loads it: with its queue (V4 sets the tick's copy of the header after its commit).
        item = await db.scalar(
            select(PrintQueueItem).options(selectinload(PrintQueueItem.queue)).where(PrintQueueItem.id == judged_id)
        )
        basis = row_basis(item)
        if name == "V1":
            return await scheduler._verdict_skip(db, item, basis, item.queue_id)
        if name == "V2":
            return await scheduler._fail_source_item(db, item, "source_unreadable", basis=basis)
        if name == "V3":
            return await scheduler._fail_item(
                db, item, "refused before the claim", "dispatch_internal_error", basis=basis
            )
        return await scheduler._verdict_storage_full(db, item, basis, item.queue_id)


# What each verdict leaves on the judged row and the header (status, is_paused) when it was written: V3 halts its
# own queue to error, V4 pauses it.
WRITTEN = {
    "V1": ("skipped", ("idle", False)),
    "V2": ("failed", ("idle", False)),
    "V3": ("failed", ("error", False)),
    "V4": ("pending", ("idle", True)),
}

# writer: (the row it acts on, its request, the barrier inside its lane, the verdict's answer when it went first)
WRITERS = {
    "start": ("staged", lambda c, r: c.post(f"/api/v1/queue/{r}/start"), (held_inside, "queue_claim_scope"), False),
    "manual_start": (
        "staged",
        lambda c, r: c.patch(f"/api/v1/queue/{r}/manual-start"),
        (held_after, "lock_named_rows"),
        False,
    ),
    # Unskip, like Retry, files its row at the end: the judged row stays the head.
    "unskip": ("skipped", lambda c, r: c.post(f"/api/v1/queue/{r}/unskip"), (held_inside, "claim_lanes"), True),
    "scheduled_time": (
        "scheduled",
        lambda c, r: c.patch(f"/api/v1/queue/{r}", json={"scheduled_time": None}),
        (held_inside, "claim_lanes"),
        False,
    ),
    "reorder": (
        "behind",
        lambda c, r: c.post(f"/api/v1/queue/{r}/reorder", params={"direction": "up"}),
        (held_inside, "queue_scope_lock"),
        False,
    ),
    "retry": ("failed", lambda c, r: c.post(f"/api/v1/queue/{r}/retry"), (held_inside, "claim_lanes"), True),
}

PAIRS = [(name, "start") for name in WRITTEN] + [("V1", writer) for writer in WRITERS if writer != "start"]


async def judged_and_header(factory, queue_id, judged_id):
    async with factory() as db:
        header = await db.get(PrinterQueue, queue_id)
        return (await db.get(PrintQueueItem, judged_id)).status, (header.status, header.is_paused)


# The single reorder serialises through ``queue_scope_lock``: on SQLite that is the writer alone (no process lock),
# so its wait -- and the verdict's wait behind it -- is SQLite's, which only the task's being unfinished shows.
SQLITE_WRITER_ONLY = {"reorder"}


def lane_of(writer, queue_id):
    return None if writer in SQLITE_WRITER_ONLY else queue_id


@pytest.mark.parametrize(("name", "writer"), PAIRS)
async def test_a_head_writer_meeting_the_verdict_waits_on_the_lane_then_applies(
    name, writer, test_engine, factory, db_session, printer_factory, async_client, monkeypatch
):
    front, request, _barrier, _first = WRITERS[writer]
    queue_id, row_id, judged_id = await a_gated_head(db_session, printer_factory, front)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, PrintScheduler, "_prove_verdict")
    judging = asyncio.create_task(verdict(factory, name, judged_id))
    writing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        writing = asyncio.create_task(request(async_client, row_id))
        await a_waiter_appears(test_engine, factory, writing, lane_of(writer, queue_id), lane=queue_id)
        release.set()
        outcome = await asyncio.wait_for(judging, 20)
        response = await asyncio.wait_for(writing, 20)
    finally:
        release.set()
        await asyncio.gather(judging, *([writing] if writing else []), return_exceptions=True)
    assert outcome.written, outcome
    assert response.status_code == 200, response.text
    status, paused = await judged_and_header(factory, queue_id, judged_id)
    assert (status, paused) == WRITTEN[name]


@pytest.mark.parametrize(("name", "writer"), PAIRS)
async def test_the_verdict_meeting_a_head_writer_waits_on_the_lane_then_judges_the_new_head(
    name, writer, test_engine, factory, db_session, printer_factory, async_client, monkeypatch
):
    front, request, (barrier, held), still_head = WRITERS[writer]
    queue_id, row_id, judged_id = await a_gated_head(db_session, printer_factory, front)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = barrier(monkeypatch, queue_ops, held)
    writing = asyncio.create_task(request(async_client, row_id))
    judging = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        judging = asyncio.create_task(verdict(factory, name, judged_id))
        await a_waiter_appears(test_engine, factory, judging, lane_of(writer, queue_id), lane=queue_id)
        release.set()
        response = await asyncio.wait_for(writing, 20)
        outcome = await asyncio.wait_for(judging, 20)
    finally:
        release.set()
        await asyncio.gather(writing, *([judging] if judging else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    if still_head:
        assert outcome.written, outcome
    else:
        assert (outcome.written, outcome.code) == (False, "not_first_runnable")
        assert (await judged_and_header(factory, queue_id, judged_id)) == ("pending", ("idle", False)), (
            "nothing written"
        )


async def test_a_row_ahead_that_becomes_due_while_the_verdict_waits_is_seen_after_the_grant(
    test_engine, factory, db_session, printer_factory, monkeypatch
):
    """The verdict's ``now`` is read after the lane's guards were granted, never before the wait."""
    due = utcnow() + timedelta(seconds=2)
    queue_id, front_id, judged_id = await a_gated_head(db_session, printer_factory, "scheduled", scheduled_time=due)
    async with factory() as db:
        assert await db.scalar(first_runnable_query(queue_id, datetime.now(timezone.utc), set())) == judged_id
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    # The lane's holder claims nothing (no row 0): it only keeps the verdict waiting past the moment the row ahead
    # becomes due.
    holder = asyncio.create_task(claim_holding_the_lane(factory, queue_id, 0, reached, release))
    judging = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        judging = asyncio.create_task(verdict(factory, "V1", judged_id))
        await a_waiter_appears(test_engine, factory, judging, queue_id)
        await asyncio.sleep(max(0.0, (due - utcnow()).total_seconds()) + 0.5)
        release.set()
        await asyncio.wait_for(holder, 20)
        outcome = await asyncio.wait_for(judging, 20)
    finally:
        release.set()
        await asyncio.gather(holder, *([judging] if judging else []), return_exceptions=True)
    assert (outcome.written, outcome.code) == (False, "not_first_runnable")
    async with factory() as db:
        assert (await db.get(PrintQueueItem, judged_id)).status == "pending"
