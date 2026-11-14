"""Run actual publication and duplicate return against disposable native PostgreSQL."""

import asyncio

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.settings import Settings
from backend.app.services import auto_queue_return as returns
from backend.tests.integration.swap_terminal_support import make_rig  # noqa: F401
from backend.tests.integration.test_auto_queue_return_publication import (
    ready,
    test_two_connections_publish_once_and_retain_failed_owner as assert_duplicate,
    test_two_returns_then_third_physical_failure_stays_failed as assert_limit,
)
from backend.tests.integration.test_auto_queue_return_settlement import failed_auto_run  # noqa: F401
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def dialect(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))


async def test_duplicate_return_on_postgres(failed_auto_run):
    await assert_duplicate(failed_auto_run)


async def test_limit_on_postgres(failed_auto_run):
    await assert_limit(failed_auto_run)


async def test_toggle_readers_share_but_off_save_waits(test_engine, failed_auto_run, monkeypatch):
    rig, _, _, _, _ = failed_auto_run
    receipt_id, _ = await ready(failed_auto_run)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        candidate = returns._receipt_candidate(await db.get(AutoQueueReturn, receipt_id))
    arrived, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    readers = []

    async def owner_barrier(db, captured):
        readers.append(db)
        if len(readers) == 2:
            arrived.set()
        await release.wait()
        raise returns.ReturnRefused("test_boundary")

    monkeypatch.setattr(returns, "_owner", owner_barrier)

    async def read():
        async with factory() as db:
            with pytest.raises(returns.ReturnRefused, match="test_boundary"):
                await returns._locked_rows(db, candidate)

    async def save_off():
        async with factory() as db:
            await db.execute(update(Settings).where(Settings.key == returns.SETTING_KEY).values(value="false"))
            await db.commit()
        saved.set()

    first, second = asyncio.create_task(read()), asyncio.create_task(read())
    await asyncio.wait_for(arrived.wait(), 5)
    save = asyncio.create_task(save_off())
    await asyncio.sleep(0.1)
    assert not saved.is_set()
    release.set()
    await asyncio.gather(first, second, save)
    assert saved.is_set()
    async with factory() as db:
        with pytest.raises(returns.ReturnRefused, match="disabled"):
            await returns._locked_rows(db, candidate)
    assert len(readers) == 2
