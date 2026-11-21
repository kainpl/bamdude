"""Two real sessions meet on a lane or on router rows, both orders (G03 B; TA17, TA20 (c), TA22, TA24, TA25, TA26).

On PostgreSQL (``DISPATCH_TEST_POSTGRES_URL``) the waiter is observed in ``pg_stat_activity`` on a lock -- the
process lane lock is bypassed there so the database guard is what it waits on; on a file-backed SQLite it waits on
the lane's process lock or SQLite's writer. No order ends in a deadlock (40P01), a hang or a partial write: the
second writer either waits and then refuses, or waits and then acts on what the first committed.

The claim here is the claim's lock SHAPE -- its lane, its header ``FOR NO KEY UPDATE``, its job row ``FOR NO KEY
UPDATE``, then a compare-and-set on a runnable ``pending`` row -- not the scheduler's whole claim: what is under test is
the other writer's protocol against it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
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

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def group(item_ids):
    from backend.app.api.routes.print_queue import group_items_into_batch
    from backend.app.core import database
    from backend.app.schemas.print_queue import PrintQueueBatchCreate

    async with database.async_session() as db:
        return await group_items_into_batch(PrintQueueBatchCreate(item_ids=item_ids), db=db, _=None)


async def test_a_group_meeting_a_claim_waits_on_the_lane_then_refuses(
    test_engine, db_session, printer_factory, monkeypatch
):
    queue_id, ids = await pending_rows(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, ids[1], reached, release))
    grouping = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        grouping = asyncio.create_task(group(ids))
        await a_waiter_appears(test_engine, factory, grouping, queue_id)
        release.set()
        assert await asyncio.wait_for(claim, 20) == 1
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(grouping, 20)
        assert refused.value.status_code == 409
    finally:
        release.set()
        await asyncio.gather(claim, *([grouping] if grouping else []), return_exceptions=True)
    async with factory() as db:
        rows = {row.id: row for row in await db.scalars(select(PrintQueueItem).where(PrintQueueItem.id.in_(ids)))}
    assert (rows[ids[1]].status, rows[ids[0]].batch_id, rows[ids[1]].batch_id) == ("printing", None, None)


async def test_a_claim_meeting_a_group_waits_on_the_lane_then_claims_the_grouped_row(
    test_engine, db_session, printer_factory, monkeypatch
):
    queue_id, ids = await pending_rows(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_ops, "lock_named_rows")
    grouping = asyncio.create_task(group(ids))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, ids[0], asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, queue_id)
        release.set()
        grouped = await asyncio.wait_for(grouping, 20)
        assert await asyncio.wait_for(claim, 20) == 1
    finally:
        release.set()
        await asyncio.gather(grouping, *([claim] if claim else []), return_exceptions=True)
    async with factory() as db:
        rows = {row.id: row for row in await db.scalars(select(PrintQueueItem).where(PrintQueueItem.id.in_(ids)))}
    assert rows[ids[0]].status == "printing"
    assert rows[ids[0]].batch_id == rows[ids[1]].batch_id == grouped["batch_id"]


async def skip(item_id):
    from backend.app.api.routes.print_queue import skip_item
    from backend.app.core import database

    async with database.async_session() as db:
        return await skip_item(item_id, db=db, _=None)


async def toggle(item_id):
    from backend.app.api.routes.print_queue import toggle_manual_start
    from backend.app.core import database

    async with database.async_session() as db:
        return await toggle_manual_start(item_id, db=db, _=None)


# The writer, the queue_ops function it is held after inside its lane, the error code it refuses with after a claim,
# and the row (status, manual_start) once the writer went first -- the claim then finds it no longer runnable.
LIGHT_WRITERS = {
    "skip": (skip, "set_status", "recovery_changed", ("skipped", False)),
    "manual_start": (toggle, "lock_named_rows", "queue_item_changed", ("pending", True)),
}


@pytest.mark.parametrize("name", sorted(LIGHT_WRITERS))
async def test_a_light_writer_meeting_a_claim_waits_on_the_lane_then_refuses(
    name, test_engine, db_session, printer_factory, monkeypatch
):
    writer, _held, code, _after = LIGHT_WRITERS[name]
    queue_id, ids = await pending_rows(db_session, printer_factory, count=1)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, ids[0], reached, release))
    writing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        writing = asyncio.create_task(writer(ids[0]))
        await a_waiter_appears(test_engine, factory, writing, queue_id)
        release.set()
        assert await asyncio.wait_for(claim, 20) == 1
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(writing, 20)
        assert (refused.value.status_code, refused.value.detail["error"]) == (409, code)
    finally:
        release.set()
        await asyncio.gather(claim, *([writing] if writing else []), return_exceptions=True)
    async with factory() as db:
        row = await db.get(PrintQueueItem, ids[0])
    assert (row.status, row.manual_start) == ("printing", False)


@pytest.mark.parametrize("name", sorted(LIGHT_WRITERS))
async def test_a_claim_meeting_a_light_writer_waits_on_the_lane_then_finds_it_not_runnable(
    name, test_engine, db_session, printer_factory, monkeypatch
):
    writer, held, _code, after = LIGHT_WRITERS[name]
    queue_id, ids = await pending_rows(db_session, printer_factory, count=1)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_ops, held)
    writing = asyncio.create_task(writer(ids[0]))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, ids[0], asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, queue_id)
        release.set()
        await asyncio.wait_for(writing, 20)
        assert await asyncio.wait_for(claim, 20) == 0
    finally:
        release.set()
        await asyncio.gather(writing, *([claim] if claim else []), return_exceptions=True)
    async with factory() as db:
        row = await db.get(PrintQueueItem, ids[0])
    assert (row.status, row.manual_start) == after


async def a_job_of_an_archive(db_session, printer_factory, archive_factory) -> tuple[int, int, int]:
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    archive = await archive_factory(queue_id, status="completed")
    row = PrintQueueItem(queue_id=queue_id, position=1, status="pending", archive_id=archive.id)
    db_session.add(row)
    await db_session.commit()
    return queue_id, archive.id, row.id


async def test_an_archive_trash_meeting_a_claim_waits_on_the_lane_then_refuses_the_running_job(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    """TA24: the claim holds the job's lane; the trash waits on it, then finds the job printing -- 409, nothing
    cancelled, the archive not trashed."""
    from backend.app.models.archive import PrintArchive

    queue_id, archive_id, item_id = await a_job_of_an_archive(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, item_id, reached, release))
    trash = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        trash = asyncio.create_task(async_client.delete(f"/api/v1/archives/{archive_id}"))
        await a_waiter_appears(test_engine, factory, trash, queue_id)
        release.set()
        assert await asyncio.wait_for(claim, 20) == 1
        response = await asyncio.wait_for(trash, 20)
    finally:
        release.set()
        await asyncio.gather(claim, *([trash] if trash else []), return_exceptions=True)
    assert response.status_code == 409, response.text
    async with factory() as db:
        assert (await db.get(PrintQueueItem, item_id)).status == "printing"
        assert (await db.get(PrintArchive, archive_id)).deleted_at is None


async def test_a_claim_meeting_an_archive_trash_waits_on_the_lane_then_finds_the_job_cancelled(
    test_engine, db_session, printer_factory, archive_factory, async_client, monkeypatch
):
    """TA24 reversed: the trash stopped after its dependents' re-read under the lanes; the claim waits on the lane;
    the trash cancels the pending job and commits; the claim's compare-and-set finds nothing runnable."""
    from backend.app.models.archive import PrintArchive
    from backend.app.services import archive_delete_scope

    queue_id, archive_id, item_id = await a_job_of_an_archive(db_session, printer_factory, archive_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, archive_delete_scope.ArchiveDeletePlan, "recheck")
    trash = asyncio.create_task(async_client.delete(f"/api/v1/archives/{archive_id}"))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, item_id, asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, queue_id)
        release.set()
        response = await asyncio.wait_for(trash, 20)
        assert await asyncio.wait_for(claim, 20) == 0
    finally:
        release.set()
        await asyncio.gather(trash, *([claim] if claim else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.get(PrintQueueItem, item_id)).status == "cancelled"
        assert (await db.get(PrintArchive, archive_id)).deleted_at is not None


async def a_run_and_an_older_failure(db_session, printer_factory, archive_factory):
    """Printer P printing archive X through row R (its header names R), and an older failed row F of P's queue."""
    from datetime import datetime, timezone

    from backend.app.models.user import User
    from backend.app.services.swap_source_declarations import Actor
    from backend.tests.auth_helpers import mint_test_session

    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    archive = await archive_factory(queue_id, status="printing", started_at=datetime.now(timezone.utc))
    running = PrintQueueItem(
        queue_id=queue_id, position=0, status="printing", archive_id=archive.id, started_at=datetime.now(timezone.utc)
    )
    failed = PrintQueueItem(queue_id=queue_id, position=2, status="failed", require_previous_success=True)
    user = User(username="retrier", password_hash="unused", role="admin", is_active=True)
    db_session.add_all([running, failed, user])
    await db_session.flush()
    header = await db_session.get(PrinterQueue, queue_id)
    header.status, header.current_item_id = "printing", running.id
    issued = await mint_test_session(db_session, user=user)
    await db_session.commit()
    actor = Actor("user", user.id, user.username, capture=issued.capture)
    return queue_id, archive.id, running.id, failed.id, actor


async def reconcile(factory, archive_id):
    from backend.app.models.archive import PrintArchive
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        return await _reconcile_complete_archive(db, archive, status="completed", uncertain=False)


async def retry(factory, queue_id, item_id, actor):
    from backend.app.services.queue_bulk_retry import retry_block

    async with factory() as db:
        return await retry_block(db, queue_id, [item_id], actor=actor, granted_scope=None, key_hash=None)


@pytest.fixture
def no_plate_gate(monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    async def armed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(printer_manager, "arm_awaiting_plate_clear", armed)


async def test_a_bulk_retry_meeting_the_reconciliation_close_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch, no_plate_gate
):
    """TA22: the close stopped after its header write; the bulk Retry of the older failed row waits on the lane --
    never on the header or a row -- and then retries it on the released queue."""
    queue_id, archive_id, running_id, failed_id, actor = await a_run_and_an_older_failure(
        db_session, printer_factory, archive_factory
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_counters, "set_queue_idle")
    closing = asyncio.create_task(reconcile(factory, archive_id))
    retrying = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        retrying = asyncio.create_task(retry(factory, queue_id, failed_id, actor))
        await a_waiter_appears(test_engine, factory, retrying, queue_id)
        release.set()
        assert await asyncio.wait_for(closing, 20) == archive_id
        assert (await asyncio.wait_for(retrying, 20))["retried"] == [failed_id]
    finally:
        release.set()
        await asyncio.gather(closing, *([retrying] if retrying else []), return_exceptions=True)
    async with factory() as db:
        header = await db.get(PrinterQueue, queue_id)
        assert (header.status, header.current_item_id) == ("idle", None)
        assert (await db.get(PrintQueueItem, failed_id)).status == "pending"
        closed = await db.get(PrintQueueItem, running_id)
        assert closed is None or closed.status == "completed"


async def test_the_reconciliation_close_meeting_a_bulk_retry_waits_on_the_lane(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch, no_plate_gate
):
    """TA22 reversed: the bulk Retry holds the lane and the failed row; the close waits on the lane guard (on SQLite
    on the writer), then closes its own row and releases the header it names."""
    queue_id, archive_id, running_id, failed_id, actor = await a_run_and_an_older_failure(
        db_session, printer_factory, archive_factory
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, queue_counters, "update_queue_counters")
    retrying = asyncio.create_task(retry(factory, queue_id, failed_id, actor))
    closing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        closing = asyncio.create_task(reconcile(factory, archive_id))
        # SQLite: the close's first commit already waits for the writer the retry holds, before it asks for the lane.
        await a_waiter_appears(test_engine, factory, closing)
        release.set()
        assert (await asyncio.wait_for(retrying, 20))["retried"] == [failed_id]
        assert await asyncio.wait_for(closing, 20) == archive_id
    finally:
        release.set()
        await asyncio.gather(retrying, *([closing] if closing else []), return_exceptions=True)
    async with factory() as db:
        header = await db.get(PrinterQueue, queue_id)
        assert (header.status, header.current_item_id) == ("idle", None)
        assert (await db.get(PrintQueueItem, failed_id)).status == "pending"
        closed = await db.get(PrintQueueItem, running_id)
        assert closed is None or closed.status == "completed"


async def test_a_stop_meeting_the_next_claim_waits_on_the_lane_then_refuses(
    test_engine, db_session, printer_factory, async_client, monkeypatch
):
    """TA20 (c): the Stop read attempt T; a no-effect deferral sent the row back (proof T); the next claim is stopped
    inside its lane after its header lock when the Stop resumes -> the Stop waits on the lane, never on the header or
    the job row; the claim commits its own token; the Stop then writes nothing (409 ``stop_target_changed``)."""
    from datetime import datetime, timedelta

    from backend.app.services import print_control
    from backend.app.services.filament_deferred import defer_claim
    from backend.app.services.printer_manager import printer_manager

    started = datetime(2026, 11, 20, 10, 0, 0)
    newer = started + timedelta(minutes=5)
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    row = PrintQueueItem(queue_id=queue_id, position=0, status="printing", started_at=started)
    db_session.add(row)
    await db_session.flush()
    header = await db_session.get(PrinterQueue, queue_id)
    header.status, header.current_item_id = "printing", row.id
    await db_session.commit()
    item_id = row.id
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim: list[asyncio.Task] = []

    async def fresh(_printer_id):
        async with factory() as db:
            await defer_claim(db, item_id=item_id, started_at=started, queue_id=queue_id, reason="feed_settle_timeout")
        claim.append(
            asyncio.create_task(
                claim_holding_the_lane(
                    factory, queue_id, item_id, reached, release, started_at=newer, deferred_claim_token=None
                )
            )
        )
        await asyncio.wait_for(reached.wait(), 10)
        return True

    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", fresh)
    monkeypatch.setattr(print_control, "stop_print_by_user", lambda _printer_id, **_kwargs: True)
    stopping = asyncio.create_task(async_client.post(f"/api/v1/queue/{item_id}/stop"))
    try:
        await asyncio.wait_for(reached.wait(), 10)
        # The claim is started from inside the Stop's own flow (the reconnect it waits on), so it is named.
        await a_waiter_appears(test_engine, factory, stopping, queue_id, behind=claim[0])
        release.set()
        assert await asyncio.wait_for(claim[0], 20) == 1
        response = await asyncio.wait_for(stopping, 20)
    finally:
        release.set()
        await asyncio.gather(stopping, *claim, return_exceptions=True)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "stop_target_changed"
    async with factory() as db:
        current = await db.get(PrintQueueItem, item_id)
        assert (current.status, current.started_at) == ("printing", newer)


async def an_order_with_a_job(committing_client, db_session, printer_factory) -> tuple[int, int, int]:
    created = await committing_client.post("/api/v1/projects/", json={"name": "Raced"})
    assert created.status_code in (200, 201), created.text
    project_id = created.json()["id"]
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    row = PrintQueueItem(queue_id=queue_id, position=1, status="pending", project_id=project_id)
    db_session.add(row)
    await db_session.commit()
    return project_id, queue_id, row.id


async def test_an_order_deletion_meeting_a_claim_waits_on_the_lane_then_unlinks_the_running_job(
    test_engine, db_session, printer_factory, committing_client, monkeypatch
):
    """TA26: the claim holds the job's lane; the order deletion takes its early Workshop locks, waits on the lane, then
    unlinks the job the claim made printing. No cycle: the claim never asks for an order row."""
    from backend.app.models.project import Project

    project_id, queue_id, item_id = await an_order_with_a_job(committing_client, db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = asyncio.Event(), asyncio.Event()
    claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, item_id, reached, release))
    deleting = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        deleting = asyncio.create_task(committing_client.delete(f"/api/v1/projects/{project_id}"))
        await a_waiter_appears(test_engine, factory, deleting, queue_id)
        release.set()
        assert await asyncio.wait_for(claim, 20) == 1
        response = await asyncio.wait_for(deleting, 20)
    finally:
        release.set()
        await asyncio.gather(claim, *([deleting] if deleting else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        job = await db.get(PrintQueueItem, item_id)
        assert (job.status, job.project_id) == ("printing", None)
        assert await db.get(Project, project_id) is None


async def test_a_claim_meeting_an_order_deletion_waits_on_the_lane_then_claims_the_unlinked_job(
    test_engine, db_session, printer_factory, committing_client, monkeypatch
):
    """TA26 reversed: the deletion stopped under its lanes after its footprint re-read; the claim waits on the lane,
    then claims the job the deletion unlinked."""
    from backend.app.api.routes import projects

    project_id, queue_id, item_id = await an_order_with_a_job(committing_client, db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, projects, "_require_footprint")
    deleting = asyncio.create_task(committing_client.delete(f"/api/v1/projects/{project_id}"))
    claim = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        claim = asyncio.create_task(claim_holding_the_lane(factory, queue_id, item_id, asyncio.Event(), go()))
        await a_waiter_appears(test_engine, factory, claim, queue_id)
        release.set()
        response = await asyncio.wait_for(deleting, 20)
        assert await asyncio.wait_for(claim, 20) == 1
    finally:
        release.set()
        await asyncio.gather(deleting, *([claim] if claim else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        job = await db.get(PrintQueueItem, item_id)
        assert (job.status, job.project_id) == ("printing", None)


async def test_a_batch_cancel_meeting_the_waiting_batch_waits_on_the_router_rows(test_engine, db_session, monkeypatch):
    """TA25: the pass's waiting batch holds router rows {1, 2} (ascending); the batch cancel of the same rows waits on
    a row lock -- never a cycle -- and then removes them."""
    from backend.app.api.routes.auto_queue import cancel_auto_queue_batch
    from backend.app.core import database
    from backend.app.services.auto_queue_eligibility import router_row_view, write_router_rows

    rows = [AutoQueueItem(status="pending", position=i, batch_id="b-1") for i in (2, 1)]
    db_session.add_all(rows)
    await db_session.commit()
    for row in rows:
        await db_session.refresh(row)
    ids = sorted(row.id for row in rows)
    views = {row.id: router_row_view(row) for row in rows}
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    reached, release = asyncio.Event(), asyncio.Event()
    real = queue_counters.lock_auto_rows
    armed = [True]

    async def held_by_the_batch(db, wanted):
        locked = await real(db, wanted)
        if armed[0]:
            armed[0] = False
            reached.set()
            await asyncio.wait_for(release.wait(), 20)
        return locked

    monkeypatch.setattr(queue_counters, "lock_auto_rows", held_by_the_batch)

    async def waiting_batch():
        async with factory() as db:
            return await write_router_rows(db, {i: (views[i], {"waiting_reason": "no printer"}) for i in ids})

    async def cancel():
        async with database.async_session() as db:
            return await cancel_auto_queue_batch("b-1", db=db, _=None)

    batch = asyncio.create_task(waiting_batch())
    cancelling = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        cancelling = asyncio.create_task(cancel())
        await a_waiter_appears(test_engine, factory, cancelling, on=ROW_WAIT)
        release.set()
        assert await asyncio.wait_for(batch, 20) == dict.fromkeys(ids, "written")
        cancelled = await asyncio.wait_for(cancelling, 20)
        assert cancelled.affected == 2
    finally:
        release.set()
        await asyncio.gather(batch, *([cancelling] if cancelling else []), return_exceptions=True)
    async with factory() as db:
        assert list(await db.scalars(select(AutoQueueItem.id).where(AutoQueueItem.id.in_(ids)))) == []
