"""m271: a printer's place in the AutoQueue printer order (C01, upstream #2915); nobody has one after the upgrade."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m271_printer_dispatch_priority as m271

pytestmark = pytest.mark.asyncio


async def test_the_upgrade_adds_a_nullable_priority_and_runs_twice():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE printers (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql("INSERT INTO printers (id) VALUES (1)")
            await m271.upgrade(conn)
            await m271.upgrade(conn)  # repeat-safe
            value = (await conn.execute(text("SELECT dispatch_priority FROM printers WHERE id = 1"))).scalar_one()
            assert value is None
            await conn.exec_driver_sql("UPDATE printers SET dispatch_priority = 0 WHERE id = 1")
            assert (await conn.execute(text("SELECT dispatch_priority FROM printers"))).scalar_one() == 0
    finally:
        await engine.dispose()


def test_the_migration_is_named_and_numbered():
    assert m271.version == 271
    assert m271.name == "printer_dispatch_priority"
