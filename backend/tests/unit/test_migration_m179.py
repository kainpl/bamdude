"""m179 creates the ``cameras`` table — the cameras that belong to no printer."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m179_cameras as m179

_LOCATIONS = "CREATE TABLE printer_locations (id INTEGER PRIMARY KEY, name VARCHAR(100))"


async def _names(conn, kind: str) -> set[str]:
    rows = await conn.execute(text(f"SELECT name FROM sqlite_master WHERE type = '{kind}'"))
    return {r[0] for r in rows.fetchall()}


@pytest.mark.asyncio
async def test_upgrade_creates_the_table_with_its_index_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_LOCATIONS)
        await m179.upgrade(conn)
        await m179.upgrade(conn)

        assert "cameras" in await _names(conn, "table")
        assert "ix_cameras_location_id" in await _names(conn, "index")
        columns = {r[1]: r for r in (await conn.execute(text("PRAGMA table_info(cameras)"))).fetchall()}
        assert {
            "id",
            "name",
            "camera_type",
            "url",
            "snapshot_url",
            "rotation",
            "enabled",
            "location_id",
            "created_at",
            "updated_at",
        } <= set(columns)
        # A camera nobody filed under a place is a valid camera.
        assert columns["location_id"][3] == 0, "location_id is optional"
        assert columns["name"][3] == 1, "name is required"
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_name_is_unique_and_a_camera_needs_no_location(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_LOCATIONS)
        await m179.upgrade(conn)
        insert = (
            "INSERT INTO cameras (name, camera_type, url, rotation, enabled, created_at, updated_at) "
            "VALUES (:n, 'mjpeg', 'http://cam/stream', 0, 1, '2026-01-01', '2026-01-01')"
        )
        await conn.execute(text(insert), {"n": "Shelf"})
        with pytest.raises(Exception, match="UNIQUE"):
            await conn.execute(text(insert), {"n": "Shelf"})
    await engine.dispose()
