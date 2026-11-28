"""m273: a printer's optional WLED preset configuration (N02, upstream #3124); none after the upgrade."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m273_printer_wled_config as m273


async def _columns(conn) -> list[str]:
    return [row[1] for row in (await conn.execute(text("PRAGMA table_info(printers)"))).all()]


@pytest.mark.asyncio
async def test_the_upgrade_adds_a_nullable_json_column_and_runs_twice():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE printers (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql("INSERT INTO printers (id) VALUES (1)")
            await m273.upgrade(conn)
            await m273.upgrade(conn)  # repeat-safe
            assert "wled_config" in await _columns(conn)
            assert (await conn.execute(text("SELECT wled_config FROM printers"))).scalar_one() is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_database_from_bambuddy_already_has_the_column():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE printers (id INTEGER PRIMARY KEY, wled_config JSON)")
            await conn.exec_driver_sql("""INSERT INTO printers (id, wled_config) VALUES (1, '{"enabled": true}')""")
            await m273.upgrade(conn)
            assert (await _columns(conn)).count("wled_config") == 1
            assert (await conn.execute(text("SELECT wled_config FROM printers"))).scalar_one() == '{"enabled": true}'
    finally:
        await engine.dispose()


def test_the_migration_is_named_and_numbered():
    assert m273.version == 273
    assert m273.name == "printer_wled_config"
