"""m171 adds the two rebalancing stamps and nothing else — nullable, no backfill, idempotent."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m171_auto_queue_rebalance as m171


@pytest.mark.asyncio
async def test_adds_both_columns_once_and_leaves_existing_rows_null(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m171.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, status TEXT)")
        await conn.exec_driver_sql("INSERT INTO auto_queue_items (id, status) VALUES (1, 'pending')")
    async with engine.begin() as conn:
        await m171.upgrade(conn)
        await m171.upgrade(conn)  # add_column skips a column that exists — a re-run (DEBUG=true) must not fail
        cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(auto_queue_items)")).all()}
        row = (await conn.exec_driver_sql("SELECT rebalanced_at, rebalanced_from_model FROM auto_queue_items")).one()
    await engine.dispose()
    assert {"rebalanced_at", "rebalanced_from_model"} <= cols
    assert tuple(row) == (None, None)
    assert (m171.version, m171.name) == (171, "auto_queue_rebalance")
