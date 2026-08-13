"""m135 adds the per-item macro selection.

Nullable and empty on every existing row: an item queued before this feature
never had a selection, and under opt-in that means it runs no macros.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m135_queue_selected_macros as m135

_TABLES_BEFORE_M135 = (
    """
CREATE TABLE print_queue (
    id INTEGER PRIMARY KEY,
    queue_id INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    execute_swap_macros BOOLEAN DEFAULT 1,
    swap_macro_events TEXT
)
""",
    """
CREATE TABLE auto_queue_items (
    id INTEGER PRIMARY KEY,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    execute_swap_macros BOOLEAN DEFAULT 1,
    swap_macro_events TEXT
)
""",
)


async def _prepared(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        for ddl in _TABLES_BEFORE_M135:
            await conn.exec_driver_sql(ddl)
    return engine


async def _columns(engine, table: str) -> set[str]:
    async with engine.begin() as conn:
        return await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns(table)})


def test_the_migration_declares_its_version_and_name():
    assert m135.version == 135
    assert m135.name == "queue_selected_macros"


@pytest.mark.asyncio
async def test_the_prepared_database_really_lacks_the_column(tmp_path):
    """A guard on the guard: build it from today's model and the assertion
    below would pass without the migration doing anything."""
    engine = await _prepared(tmp_path / "guard.db")
    assert "selected_macro_ids" not in await _columns(engine, "print_queue")
    assert "selected_macro_ids" not in await _columns(engine, "auto_queue_items")
    await engine.dispose()


@pytest.mark.asyncio
async def test_it_adds_the_column_to_both_queues(tmp_path):
    """The auto-queue row is the one the distributor copies from, so a
    selection that exists only on the per-printer item would be dropped on
    the way through."""
    engine = await _prepared(tmp_path / "a.db")
    async with engine.begin() as conn:
        await m135.upgrade(conn)

    assert "selected_macro_ids" in await _columns(engine, "print_queue")
    assert "selected_macro_ids" in await _columns(engine, "auto_queue_items")
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """DEBUG=true re-runs the newest migration on every start."""
    engine = await _prepared(tmp_path / "b.db")
    async with engine.begin() as conn:
        await m135.upgrade(conn)
        await m135.upgrade(conn)
    await engine.dispose()
