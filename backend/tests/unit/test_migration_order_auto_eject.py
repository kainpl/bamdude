"""Old jobs stay manual; re-running the additive migration preserves choices."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m197_order_auto_eject as migration


@pytest.mark.asyncio
async def test_additive_upgrade_preserves_old_jobs_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auto-eject.db'}")
    tables = {"projects": "auto_eject_enabled", "print_queue": "auto_eject", "auto_queue_items": "auto_eject"}
    try:
        async with engine.begin() as conn:
            for table in tables:
                await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                await conn.execute(text(f"INSERT INTO {table} VALUES (1)"))
            await migration.upgrade(conn)
            for table, flag in tables.items():
                assert (await conn.execute(text(f"SELECT {flag} FROM {table}"))).scalar_one() == 0
                await conn.execute(text(f"UPDATE {table} SET {flag}=TRUE WHERE id=1"))
            await migration.upgrade(conn)
            for table, flag in tables.items():
                assert (await conn.execute(text(f"SELECT {flag} FROM {table}"))).scalar_one() == 1
    finally:
        await engine.dispose()
