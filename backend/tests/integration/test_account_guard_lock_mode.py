"""The account lifetime guard never blocks a foreign-key check (G03 B, A30; TA39's lock-mode cases).

On PostgreSQL the guard is ``FOR NO KEY UPDATE``: it conflicts with every other guard of the account and with a write
or deletion of its row, but not with the ``FOR KEY SHARE`` an insert takes on a row it merely names. So a guard holder
that later waits for a lane or an order row is never waited on by an insert naming the account there -- the cycle the
review reproduced with ``FOR UPDATE``. That former statement is the negative control. SQLite serialises on its writer
and is not asked.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.auth_lifetime import _guard_accounts, capture_account
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.user import User
from backend.app.services.printer_queues import ensure_printer_queue
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ROW_WAIT = ("transactionid", "tuple")


@pytest.fixture
def postgres_only(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL lock modes; SQLite serialises on its writer")


async def account_and_queue(db_session, printer_factory) -> tuple[User, int]:
    user = User(username="guarded", password_hash="unused", role="admin", is_active=True)
    db_session.add(user)
    printer = await printer_factory()
    await ensure_printer_queue(db_session, printer.id)
    await db_session.commit()
    await db_session.refresh(user)
    return user, printer.id


async def waiters(factory) -> int:
    async with factory() as monitor:
        return await monitor.scalar(
            text(
                # By database, not application_name: a pooler does not reliably carry the client's name (see
                # test_lane_writers_concurrency.a_waiter_appears); the disposable database serves one test at a time.
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type='Lock' AND wait_event = ANY(:events)"
            ),
            {"events": list(ROW_WAIT)},
        )


async def insert_naming(factory, user_id: int, queue_id: int) -> int:
    async with factory() as db:
        row = PrintQueueItem(queue_id=queue_id, position=1, status="pending", created_by_id=user_id)
        db.add(row)
        await db.commit()
        return row.id


async def test_an_insert_naming_a_guarded_account_does_not_wait(
    postgres_only, test_engine, db_session, printer_factory
):
    user, queue_id = await account_and_queue(db_session, printer_factory)
    capture = capture_account(user)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as guard:
        await _guard_accounts(guard, [capture])
        # The guard is held (its transaction open) while a row naming the account is inserted and committed.
        item_id = await asyncio.wait_for(insert_naming(factory, user.id, queue_id), 10)
        await guard.rollback()
    async with factory() as db:
        assert (await db.get(PrintQueueItem, item_id)).created_by_id == user.id


async def test_the_former_for_update_made_that_insert_wait(postgres_only, test_engine, db_session, printer_factory):
    """Negative control: the statement the guard used before A30."""
    user, queue_id = await account_and_queue(db_session, printer_factory)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as holder:
        await holder.execute(select(User.id).where(User.account_identity == user.account_identity).with_for_update())
        inserting = asyncio.create_task(insert_naming(factory, user.id, queue_id))
        try:
            async with asyncio.timeout(10):
                while not await waiters(factory):
                    await asyncio.sleep(0.02)
            assert not inserting.done(), "the FK check waited on FOR UPDATE"
        finally:
            await holder.rollback()
        await asyncio.wait_for(inserting, 10)


async def test_two_guards_of_one_account_still_serialise(postgres_only, test_engine, db_session, printer_factory):
    user, _queue_id = await account_and_queue(db_session, printer_factory)
    capture = capture_account(user)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def second_guard():
        async with factory() as db:
            await _guard_accounts(db, [capture])
            await db.rollback()

    async with factory() as first:
        await _guard_accounts(first, [capture])
        second = asyncio.create_task(second_guard())
        try:
            async with asyncio.timeout(10):
                while not await waiters(factory):
                    await asyncio.sleep(0.02)
            assert not second.done(), "a second guard of the account waits for the first"
        finally:
            await first.rollback()
        await asyncio.wait_for(second, 10)
