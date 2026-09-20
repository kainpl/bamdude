"""m172 puts back the four indexes that table rebuilds lost."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m172_restore_lost_indexes as m172

_TABLES = (
    "CREATE TABLE auth_ephemeral_tokens (id INTEGER PRIMARY KEY, token_type VARCHAR(32), expires_at TIMESTAMP)",
    "CREATE TABLE auth_rate_limit_events (id INTEGER PRIMARY KEY, event_type VARCHAR(32), occurred_at TIMESTAMP)",
    "CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, status VARCHAR(20), position INTEGER)",
    "CREATE TABLE slicer_pipelines (id INTEGER PRIMARY KEY, is_deleted BOOLEAN)",
)


async def _index_names(conn) -> set[str]:
    rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))
    return {r[0] for r in rows.fetchall()}


@pytest.mark.asyncio
async def test_the_four_indexes_exist_afterwards(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with engine.begin() as conn:
        for ddl in _TABLES:
            await conn.exec_driver_sql(ddl)
        assert not await _index_names(conn)
        await m172.upgrade(conn)
        names = await _index_names(conn)
    await engine.dispose()

    assert {n for n, _t, _c in m172.INDEXES} <= names


@pytest.mark.asyncio
async def test_it_is_idempotent_and_survives_a_missing_table(tmp_path):
    """DEBUG=true re-runs the latest migration; and a table that a later
    migration has not created yet must not fail the chain."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    async with engine.begin() as conn:
        for ddl in _TABLES[:2]:  # only the auth tables exist
            await conn.exec_driver_sql(ddl)
        await m172.upgrade(conn)
        await m172.upgrade(conn)
        names = await _index_names(conn)
    await engine.dispose()

    assert {"ix_auth_eph_type_exp", "ix_auth_rl_type_time"} <= names
    assert "ix_auto_queue_status_position" not in names
