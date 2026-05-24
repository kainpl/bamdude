"""m079 test — add auto_linked + resolved_filament_id columns.

Scenarios:
  1. ``auto_linked`` added to both link tables; ``resolved_filament_id`` added
     to ``spool``.
  2. Idempotent — a second ``upgrade`` is a no-op.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m079_kprofile_autolink
from backend.app.migrations.helpers import column_exists


@pytest_asyncio.fixture
async def engine_pre_m079():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Post-m064 link tables WITHOUT auto_linked, and spool WITHOUT
        # resolved_filament_id — the shape m079 upgrades from.
        await conn.execute(
            text(
                "CREATE TABLE spool_k_profile ("
                "id INTEGER PRIMARY KEY, spool_id INTEGER, printer_id INTEGER, "
                "extruder INTEGER DEFAULT 0, filament_calibration_id INTEGER, created_at TEXT)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE spoolman_k_profile ("
                "id INTEGER PRIMARY KEY, spoolman_spool_id INTEGER, printer_id INTEGER, "
                "extruder INTEGER DEFAULT 0, filament_calibration_id INTEGER, created_at TEXT)"
            )
        )
        await conn.execute(text("CREATE TABLE spool (id INTEGER PRIMARY KEY, slicer_filament TEXT)"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_m079_adds_columns(engine_pre_m079):
    async with engine_pre_m079.begin() as conn:
        await m079_kprofile_autolink.upgrade(conn)

        assert await column_exists(conn, "spool_k_profile", "auto_linked")
        assert await column_exists(conn, "spoolman_k_profile", "auto_linked")
        assert await column_exists(conn, "spool", "resolved_filament_id")


@pytest.mark.asyncio
async def test_m079_idempotent(engine_pre_m079):
    async with engine_pre_m079.begin() as conn:
        await m079_kprofile_autolink.upgrade(conn)
        # Second run must not raise.
        await m079_kprofile_autolink.upgrade(conn)
        assert await column_exists(conn, "spool", "resolved_filament_id")
