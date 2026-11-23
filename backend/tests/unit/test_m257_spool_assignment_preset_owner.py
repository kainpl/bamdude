import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m257_spool_assignment_preset_owner


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_assignments():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE spool_assignment (id INTEGER PRIMARY KEY)")
            await conn.exec_driver_sql("INSERT INTO spool_assignment (id) VALUES (1)")
            await m257_spool_assignment_preset_owner.upgrade(conn)
            await m257_spool_assignment_preset_owner.upgrade(conn)
            rows = await conn.exec_driver_sql("SELECT id, preset_owner_identity FROM spool_assignment")
            assert rows.one() == (1, None)
    finally:
        await engine.dispose()
