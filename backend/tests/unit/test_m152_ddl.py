"""m152 adds users.cloud_refresh_token (spec A §3 hardening)."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m152_cloud_refresh_token as m152


@pytest.mark.asyncio
async def test_m152_adds_refresh_column(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm152.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, cloud_token VARCHAR(500))")
        await m152.upgrade(conn)
        cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(users)")).fetchall()}
        assert "cloud_refresh_token" in cols
        # idempotent (DEBUG=true re-runs the newest migration)
        await m152.upgrade(conn)
    await engine.dispose()
