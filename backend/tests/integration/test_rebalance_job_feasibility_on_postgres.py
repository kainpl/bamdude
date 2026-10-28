"""Rebalancing by job feasibility on PostgreSQL (spec rebalance-job-feasibility r2, plan §5).

The same bodies as ``test_rebalance_job_feasibility.py``, ``test_rebalance_moves_one_job_once.py``
and ``test_rebalance_tail.py`` on a real server, each test in a schema of its own: the pass's printer population and
alternative-plate reads, the previous-success lookups, the final check's fresh reads after the
target copy released the transaction, and two runs on two connections that both decided to move
one job — the row's ``FOR UPDATE`` is what lets only one of them convert it.

Skipped unless ``DISPATCH_TEST_POSTGRES_URL`` is set; the harness entry
``test_postgres_scenarios.py::test_rebalance_job_feasibility_on_postgres`` sets it. Never point it
at a shared database -- each test creates and drops its own schema, but a disposable server is the
rule.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, _strip_tz_from_params, import_all_models
from backend.tests.integration.test_rebalance_job_feasibility import *  # noqa: F403
from backend.tests.integration.test_rebalance_moves_one_job_once import *  # noqa: F403
from backend.tests.integration.test_rebalance_tail import *  # noqa: F403

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DISPATCH_TEST_POSTGRES_URL"), reason="runs only against PostgreSQL (see the docstring)"
    ),
]


@pytest.fixture
async def test_engine(monkeypatch):
    url = os.environ["DISPATCH_TEST_POSTGRES_URL"]
    schema = "rebalance_" + uuid4().hex
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema, "timezone": "UTC", "application_name": schema}}
    )
    event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    import_all_models()
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()
    await asyncio.sleep(0.1)
