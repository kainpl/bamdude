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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


@pytest.mark.asyncio
async def test_m079_seed_offline_backfill():
    """Seed links an offline-resolvable spool (GFSG99 → GFG99) to a matching
    calibration, sets resolved_filament_id, and auto-activates."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE spool (id INTEGER PRIMARY KEY, slicer_filament TEXT, resolved_filament_id TEXT)")
        )
        await conn.execute(
            text(
                "CREATE TABLE spool_k_profile ("
                "id INTEGER PRIMARY KEY, spool_id INTEGER, printer_id INTEGER, extruder INTEGER DEFAULT 0, "
                "filament_calibration_id INTEGER, auto_linked BOOLEAN NOT NULL DEFAULT 0, created_at TEXT)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE filament_calibration ("
                "id INTEGER PRIMARY KEY, printer_id INTEGER, filament_id TEXT, nozzle_diameter REAL, "
                "nozzle_volume_type TEXT, extruder_id INTEGER DEFAULT 0, is_active INTEGER DEFAULT 0)"
            )
        )
        await conn.execute(text("INSERT INTO spool (id, slicer_filament) VALUES (1, 'GFSG99')"))
        await conn.execute(
            text(
                "INSERT INTO filament_calibration "
                "(id, printer_id, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id, is_active) "
                "VALUES (5, 1, 'GFG99', 0.4, 'standard', 0, 0)"
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await m079_kprofile_autolink.seed(session_factory)

    async with engine.connect() as conn:
        resolved = (await conn.execute(text("SELECT resolved_filament_id FROM spool WHERE id=1"))).scalar()
        assert resolved == "GFG99"
        link = (
            await conn.execute(
                text("SELECT filament_calibration_id, auto_linked FROM spool_k_profile WHERE spool_id=1")
            )
        ).first()
        assert link == (5, 1)
        active = (await conn.execute(text("SELECT is_active FROM filament_calibration WHERE id=5"))).scalar()
        assert active == 1
    await engine.dispose()
