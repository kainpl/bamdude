"""The archive-facts writer boundary must not admit a second SQLite writer."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.archive_write_scope import _archive_locks, archive_write_scope

pytestmark = pytest.mark.unit


async def test_sqlite_archive_scope_waits_for_the_first_writer(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    _archive_locks.clear()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_writer() -> None:
        async with maker() as db, archive_write_scope(db, 91):
            first_entered.set()
            await release_first.wait()
            await db.commit()

    async def second_writer() -> None:
        await first_entered.wait()
        second_attempted.set()
        async with maker() as db, archive_write_scope(db, 91):
            second_entered.set()
            await db.commit()

    first = asyncio.create_task(first_writer())
    await first_entered.wait()
    second = asyncio.create_task(second_writer())
    await second_attempted.wait()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_entered.wait(), timeout=0.05)

    release_first.set()
    await asyncio.gather(first, second)
    assert second_entered.is_set()
