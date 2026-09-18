"""m178 adds ``printers.camera_light_auto`` with ``inherit`` as the standing answer."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m178_printer_camera_light_auto as m178

_PRINTERS = "CREATE TABLE printers (id INTEGER PRIMARY KEY, name VARCHAR(100))"


@pytest.mark.asyncio
async def test_upgrade_adds_the_column_with_its_default_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_PRINTERS)
        await conn.exec_driver_sql("INSERT INTO printers (id, name) VALUES (1, 'old')")
        await m178.upgrade(conn)
        await m178.upgrade(conn)
        cols = {r[1]: r for r in (await conn.execute(text("PRAGMA table_info(printers)"))).fetchall()}
        assert "camera_light_auto" in cols
        assert cols["camera_light_auto"][3] == 1, "NOT NULL"
        rows = (await conn.execute(text("SELECT camera_light_auto FROM printers"))).fetchall()
        assert rows == [("inherit",)], "an existing printer defers to the farm"
    await engine.dispose()
