"""Independent writers observe the entire retry block on SQLite and PostgreSQL."""

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.auth_lifetime import capture_key
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.user import User
from backend.app.services import queue_counters, queue_ops
from backend.app.services.printer_queues import ensure_printer_queue
from backend.app.services.queue_bulk_retry import retry_block
from backend.app.services.swap_source_declarations import Actor
from backend.tests.auth_helpers import bind_test_key, mint_test_session
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401


@pytest.mark.asyncio
async def test_final_claim_waits_and_never_sees_half_a_retry_block(
    test_engine, db_session, printer_factory, monkeypatch
):
    printer = await printer_factory()
    await ensure_printer_queue(db_session, printer.id)
    user = User(username="bulk-owner", password_hash="unused", role="admin", is_active=True)
    rows = [
        PrintQueueItem(queue_id=printer.id, position=i, status="failed", require_previous_success=True) for i in (1, 2)
    ]
    db_session.add_all([user, *rows])
    await db_session.flush()
    issued = await mint_test_session(db_session, user=user)
    queue_id, ids, actor = (
        printer.id,
        [row.id for row in rows],
        Actor("user", user.id, user.username, capture=issued.capture),
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    staged, release, claiming = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = queue_counters.update_queue_counters

    async def barrier(db, queue_id):
        await original(db, queue_id)
        staged.set()
        await asyncio.wait_for(release.wait(), 15)

    monkeypatch.setattr(queue_counters, "update_queue_counters", barrier)
    if test_engine.dialect.name == "postgresql":

        class NoProcessLock:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        monkeypatch.setitem(queue_ops._queue_claim_locks, queue_id, NoProcessLock())

    async def retry():
        async with factory() as db:
            return await retry_block(db, queue_id, ids, actor=actor, granted_scope=None, key_hash=None)

    async def claim():
        async with factory() as db:
            claiming.set()
            async with queue_ops.claim_lanes(db, [queue_id]):
                statuses = list(await db.scalars(select(PrintQueueItem.status).where(PrintQueueItem.id.in_(ids))))
                from backend.app.services.print_scheduler import scheduler

                assert await scheduler.previous_print_succeeded(db, queue_id, ids[0])
                await db.commit()
                return statuses

    retry_task = asyncio.create_task(retry())
    claim_task = None
    try:
        await asyncio.wait_for(staged.wait(), 5)
        async with factory() as reader:
            assert list(await reader.scalars(select(PrintQueueItem.status).where(PrintQueueItem.id.in_(ids)))) == [
                "failed",
                "failed",
            ]
        claim_task = asyncio.create_task(claim())
        await asyncio.wait_for(claiming.wait(), 5)
        if test_engine.dialect.name == "postgresql":
            async with factory() as monitor:
                async with asyncio.timeout(5):
                    while not await monitor.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE application_name=current_setting('application_name') AND wait_event_type='Lock'"
                        )
                    ):
                        await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0.03)
        assert not claim_task.done()
        release.set()
        assert (await asyncio.wait_for(retry_task, 5))["retried"] == ids
        assert await asyncio.wait_for(claim_task, 5) == ["pending", "pending"]
    finally:
        release.set()
        await asyncio.gather(retry_task, *([claim_task] if claim_task else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["scope", "permission", "cancelled"])
async def test_waiting_writer_uses_current_authority_and_status(
    test_engine, db_session, printer_factory, monkeypatch, change
):
    from contextlib import asynccontextmanager

    from fastapi import HTTPException

    from backend.app.models.api_key import APIKey
    from backend.app.services import queue_bulk_retry

    printer = await printer_factory()
    await ensure_printer_queue(db_session, printer.id)
    owner = User(username="bulk-key-owner", role="admin", is_active=True, groups=[])
    db_session.add(owner)
    await db_session.flush()
    key = APIKey(
        name="bulk-key", key_hash="test-only", key_prefix="bd_test", printer_ids=[printer.id], can_control_printer=True
    )
    row = PrintQueueItem(queue_id=printer.id, position=1, status="failed")
    await bind_test_key(db_session, key, owner)
    db_session.add_all([key, row])
    await db_session.commit()
    queue_id, item_id, key_id = printer.id, row.id, key.id
    actor = Actor("api_key", owner.id, "bulk-key", key_id, capture=capture_key(key))
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = queue_bulk_retry.claim_lanes

    @asynccontextmanager
    async def barrier(db, ids):
        entered.set()
        await asyncio.wait_for(release.wait(), 15)
        async with original(db, ids):
            yield

    monkeypatch.setattr(queue_bulk_retry, "claim_lanes", barrier)

    async def retry():
        async with factory() as db:
            return await retry_block(
                db, queue_id, [item_id], actor=actor, granted_scope=frozenset({queue_id}), key_hash="test-only"
            )

    task = asyncio.create_task(retry())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as editor:
            if change == "cancelled":
                current = await editor.get(PrintQueueItem, item_id)
                current.status = "cancelled"
            else:
                current = await editor.get(APIKey, key_id)
                if change == "scope":
                    current.printer_ids = []
                else:
                    current.can_queue = False
            await editor.commit()
        release.set()
        if change == "cancelled":
            result = await asyncio.wait_for(task, 5)
            assert result["retried"] == [] and result["left_in_place"] == [{"id": item_id, "reason": "not_failed"}]
        else:
            with pytest.raises(HTTPException) as refused:
                await asyncio.wait_for(task, 5)
            assert refused.value.status_code == 403
        async with factory() as reader:
            assert await reader.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id)) == (
                "cancelled" if change == "cancelled" else "failed"
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
