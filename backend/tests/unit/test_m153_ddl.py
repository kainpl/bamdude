"""m153 creates print_usage_events + the notification provider runout flag.

Fresh installs get both from create_all(); the migration covers existing DBs
and must be idempotent (DEBUG=true re-runs the newest migration on startup).
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m153_print_usage_events as m153

EXPECTED_COLUMNS = {
    "id",
    "printer_id",
    "archive_id",
    "layer_num",
    "event",
    "kind",
    "global_tray_id",
    "spool_id",
    "spoolman_spool_id",
    "created_at",
}


@pytest.mark.asyncio
async def test_m153_creates_journal_table_and_provider_flag(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm153.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE printers (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("CREATE TABLE print_archives (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql(
            "CREATE TABLE notification_providers (id INTEGER PRIMARY KEY, on_filament_low BOOLEAN DEFAULT 0)"
        )
        await m153.upgrade(conn)

        cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(print_usage_events)")).fetchall()}
        assert cols >= EXPECTED_COLUMNS

        provider_cols = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(notification_providers)")).fetchall()
        }
        assert "on_filament_runout" in provider_cols

        indexes = {row[1] for row in (await conn.exec_driver_sql("PRAGMA index_list(print_usage_events)")).fetchall()}
        assert "ix_print_usage_events_printer_id" in indexes
        assert "ix_print_usage_events_archive_id" in indexes

        # idempotent (DEBUG=true re-runs the newest migration)
        await m153.upgrade(conn)
    await engine.dispose()
