"""The pair monitor accepts only the named wait behind the named holder (G03 B; review B1-06).

``lane_pairs.postgres_edge`` is what every PostgreSQL pair leans on to say "the second writer waits for the first".
These cases pin that it cannot pass vacuously: a waiter behind a sibling transaction is not the named holder's edge,
whether the holder is named by its pid or by its task; an advisory lock of another class (or another key) is not the
lane guard in any mode; and the lane guard of the named queue behind the named holder is accepted. PostgreSQL only.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.printer import Printer
from backend.tests.integration.lane_pairs import ANY_WAIT, LANE_GUARD, LANE_WAIT, ROW_WAIT, postgres_edge
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def postgres_only(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("pg_locks and pg_blocking_pids are PostgreSQL's")


async def take_row(db, row_id: int) -> int:
    await db.execute(select(Printer.id).where(Printer.id == row_id).with_for_update())
    return await db.scalar(text("SELECT pg_backend_pid()"))


async def test_a_waiter_behind_a_sibling_is_not_the_named_holders_edge(postgres_only, test_engine, printer_factory):
    first, second = (await printer_factory()).id, (await printer_factory()).id
    sessions = async_sessionmaker(test_engine)
    ready, release = asyncio.Event(), asyncio.Event()

    async def sibling_holder():
        async with sessions() as db:
            await take_row(db, second)
            ready.set()
            await release.wait()

    async def waiter_on_the_siblings_row():
        async with sessions() as db:
            await take_row(db, second)

    async with sessions() as named:
        named_pid = await take_row(named, first)
        sibling = asyncio.create_task(sibling_holder())
        await asyncio.wait_for(ready.wait(), 5)
        waiter = asyncio.create_task(waiter_on_the_siblings_row())
        try:
            for holder in (named_pid, asyncio.current_task()):
                with pytest.raises(pytest.fail.Exception, match="no transaction of the waiter"):
                    await postgres_edge(sessions, waiter, on=ROW_WAIT, behind=holder, timeout=1.5)
            # The control: the sibling named exactly is the edge.
            edge = await postgres_edge(sessions, waiter, on=ROW_WAIT, behind=sibling)
            assert edge.locktype in ROW_WAIT
        finally:
            release.set()
            await asyncio.gather(sibling, waiter)


async def test_an_advisory_lock_of_another_class_is_no_lane_wait_in_any_mode(postgres_only, test_engine):
    sessions = async_sessionmaker(test_engine)
    ready, release = asyncio.Event(), asyncio.Event()

    async def hold(key):
        async with sessions() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(:c, :k)"), {"c": key[0], "k": key[1]})
            ready.set()
            await release.wait()

    async def wait(key):
        async with sessions() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(:c, :k)"), {"c": key[0], "k": key[1]})

    for key, accepted in (((445, 1), False), ((LANE_GUARD, 2), False), ((LANE_GUARD, 1), True)):
        ready.clear()
        release.clear()
        holder = asyncio.create_task(hold(key))
        await asyncio.wait_for(ready.wait(), 5)
        waiter = asyncio.create_task(wait(key))
        try:
            for on in (LANE_WAIT, ANY_WAIT):
                if accepted:
                    edge = await postgres_edge(sessions, waiter, 1, on=on, behind=holder)
                    assert (edge.locktype, edge.classid, edge.objid) == ("advisory", LANE_GUARD, 1)
                else:
                    with pytest.raises(pytest.fail.Exception, match="no transaction of the waiter"):
                        await postgres_edge(sessions, waiter, 1, on=on, behind=holder, timeout=1.5)
        finally:
            release.set()
            await asyncio.gather(holder, waiter)
