"""m154 adds the Orca push bookkeeping + the granted-scope column."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m154_orca_push_bookkeeping as m154


@pytest.mark.asyncio
async def test_m154_adds_orca_push_and_scope_columns(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm154.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE user_filament_presets (id INTEGER PRIMARY KEY, name VARCHAR(300))")
        await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        await m154.upgrade(conn)
        preset_cols = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(user_filament_presets)")).fetchall()
        }
        assert {
            "orca_pushed_profile_id",
            "orca_pushed_at",
            "orca_push_dirty",
            "orca_pushed_updated_time",
        } <= preset_cols
        user_cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(users)")).fetchall()}
        assert "orca_cloud_scope" in user_cols
        # idempotent (DEBUG=true re-runs the newest migration)
        await m154.upgrade(conn)
    await engine.dispose()
