import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m196_plate_detection_polygon as migration


@pytest.mark.asyncio
async def test_polygon_migration_rerun_preserves_existing_rectangle(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mask.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, plate_detection_roi_x FLOAT)"))
            await conn.execute(text("INSERT INTO printers VALUES (1, 0.0)"))
            await migration.upgrade(conn)
            await migration.upgrade(conn)
            assert (
                await conn.execute(text("SELECT plate_detection_roi_x,plate_detection_polygon FROM printers"))
            ).one() == (0, None)
    finally:
        await engine.dispose()
