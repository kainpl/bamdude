"""Helper for test_rebalance_companion_atomicity — not collected by the suite, the test spawns it.

``pytest backend/tests/integration/rebalance_kill_child.py`` in a process of its own: it builds the
small PETG farm (one job of 6 hooks, a 2-hook plate for the P1S → k = 3) in the database file
``BAMDUDE_B1_DB`` names, runs one forced move and kills the process with ``os._exit`` at the point
``BAMDUDE_B1_KILL`` names — just before, or just after, the first commit the move makes while its
blob is pinned (the old writer's conversion commit; the new writer's only one). No cleanup, no
rollback, no ``finally``: what the parent then reads from the file is what a crash leaves.
"""

import asyncio
import os

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.services import queue_rebalance, queue_sources
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 — fixtures by name
    _live,
    clean_spool,
    petg_farm,
)

pytestmark = pytest.mark.asyncio

KILLED = 17


@pytest.fixture
async def test_engine(monkeypatch):
    from sqlalchemy import text

    from backend.app.core import database as database_module
    from backend.app.core.config import settings
    from backend.app.core.database import Base, _strip_tz_from_params, configure_sqlite_connection, import_all_models

    import_all_models()
    url = os.environ.get("BAMDUDE_B1_PG_URL")
    if url:
        # A disposable server, a schema the parent named and reads afterwards.
        schema = os.environ["BAMDUDE_B1_PG_SCHEMA"]
        monkeypatch.setattr(settings, "database_url", url)
        engine = create_async_engine(
            url,
            connect_args={"server_settings": {"search_path": schema, "timezone": "UTC", "application_name": schema}},
        )
        event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(Base.metadata.create_all)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + os.environ["BAMDUDE_B1_DB"])
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database_module, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    await engine.dispose()
    await asyncio.sleep(0.1)


async def test_move_then_die(db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool):
    kill = os.environ["BAMDUDE_B1_KILL"]
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    print(f"FARM item={farm.item.id} home_file={farm.home_file.id} alt_file={farm.alt_file.id}", flush=True)
    real_commit = db_session.commit

    async def commit(*args, **kwargs):
        if queue_sources.pinned_source_ids() and kill == "before_commit":
            os._exit(KILLED)
        await real_commit(*args, **kwargs)
        if queue_sources.pinned_source_ids() and kill == "after_commit":
            os._exit(KILLED)

    monkeypatch.setattr(db_session, "commit", commit)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    raise AssertionError("the move finished without reaching its pinned commit")
