"""m151 adds the Bambu push metadata (spec B §5) to user_filament_presets."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m151_filament_push_metadata as m151


@pytest.mark.asyncio
async def test_m151_adds_push_columns(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm151.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE user_filament_presets (id INTEGER PRIMARY KEY, name VARCHAR(300))")
        await m151.upgrade(conn)
        cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(user_filament_presets)")).fetchall()}
        assert {"pushed_cloud_id", "pushed_at", "push_dirty"} <= cols
        # idempotent (DEBUG=true re-runs the newest migration)
        await m151.upgrade(conn)
    await engine.dispose()
