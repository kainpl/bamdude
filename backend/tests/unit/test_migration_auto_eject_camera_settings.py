"""An additive policy migration never changes old jobs or their mode."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m198_auto_eject_camera_settings as migration


@pytest.mark.asyncio
async def test_existing_rows_remain_unconfigured_and_upgrade_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}")
    try:
        async with engine.begin() as conn:
            for table in ("projects", "print_queue", "auto_queue_items"):
                await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, auto_eject BOOLEAN DEFAULT 0)"))
                await conn.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))
            await migration.upgrade(conn)
            for table in ("projects", "print_queue", "auto_queue_items"):
                row = (await conn.execute(text(f"SELECT auto_eject, auto_eject_settings FROM {table}"))).one()
                assert row == (0, None)
                await conn.execute(
                    text(f"UPDATE {table} SET auto_eject_settings=:policy"),
                    {"policy": '{"skip_check":false,"difference_threshold":2}'},
                )
            await migration.upgrade(conn)
            for table in ("projects", "print_queue", "auto_queue_items"):
                assert (
                    await conn.execute(text(f"SELECT auto_eject_settings FROM {table}"))
                ).scalar_one() == '{"skip_check":false,"difference_threshold":2}'
    finally:
        await engine.dispose()
