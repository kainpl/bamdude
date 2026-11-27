"""m269: a group can be limited to printers and locations; nothing is limited by the upgrade."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m269_printer_scoped_groups as m269

pytestmark = pytest.mark.asyncio


async def test_the_upgrade_adds_the_flag_and_both_tables_and_runs_twice():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT)")
            await conn.exec_driver_sql("CREATE TABLE printers (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql("CREATE TABLE printer_locations (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql("INSERT INTO groups (id, name) VALUES (1, 'Operators'), (2, 'Team')")
            await m269.upgrade(conn)
            await m269.upgrade(conn)  # repeat-safe
            flags = (await conn.execute(text("SELECT restrict_printers FROM groups ORDER BY id"))).scalars().all()
            assert [bool(f) for f in flags] == [False, False]
            await conn.exec_driver_sql("INSERT INTO group_printers (group_id, printer_id) VALUES (2, 1)")
            await conn.exec_driver_sql("INSERT INTO group_locations (group_id, location_id) VALUES (2, 1)")
            tables = set(
                (await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).scalars().all()
            )
            assert {"group_printers", "group_locations"} <= tables
    finally:
        await engine.dispose()


def test_the_migration_is_named_and_numbered():
    assert m269.version == 269
    assert m269.name == "printer_scoped_groups"
