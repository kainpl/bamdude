"""m065 test — re-key kprofile_notes from setting_id to filament_calibration_id.

Scenarios:
  1. Existing notes are dropped (clean-slate per user decision).
  2. printer_id + setting_id columns removed; filament_calibration_id added.
  3. New unique index on filament_calibration_id.
  4. Idempotent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m065_kprofile_notes_rekey


@pytest_asyncio.fixture
async def engine_with_pre_m065():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY)"))
        await conn.execute(
            text(
                """
                CREATE TABLE filament_calibration (
                    id INTEGER PRIMARY KEY,
                    printer_id INTEGER NOT NULL,
                    filament_id TEXT NOT NULL,
                    nozzle_diameter REAL NOT NULL,
                    nozzle_volume_type TEXT NOT NULL,
                    extruder_id INTEGER NOT NULL DEFAULT 0,
                    pa_k_value REAL,
                    cali_mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL,
                    created_at TEXT
                )
                """
            )
        )
        # Pre-m065 kprofile_notes shape — note the inline FK on printer_id.
        # In production this is what blocks SQLite from doing DROP COLUMN:
        # the table's own schema references printer_id from the FK clause,
        # so dropping it leaves the FK pointing at a phantom column.
        # Reproducing the FK here makes sure the migration can't pass tests
        # while still failing in real DBs.
        await conn.execute(
            text(
                """
                CREATE TABLE kprofile_notes (
                    id INTEGER PRIMARY KEY,
                    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                    setting_id TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        await conn.execute(
            text("CREATE UNIQUE INDEX ix_kprofile_notes_printer_setting ON kprofile_notes (printer_id, setting_id)")
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_m065_drops_existing_notes(engine_with_pre_m065):
    async with engine_with_pre_m065.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id) VALUES (1)"))
        await conn.execute(
            text("INSERT INTO kprofile_notes (printer_id, setting_id, note) VALUES (1, 'GFSG96', 'hello')")
        )

        await m065_kprofile_notes_rekey.upgrade(conn)

        count = (await conn.execute(text("SELECT COUNT(*) FROM kprofile_notes"))).scalar()
        assert count == 0


@pytest.mark.asyncio
async def test_m065_drops_old_columns_adds_fk_column(engine_with_pre_m065):
    async with engine_with_pre_m065.begin() as conn:
        await m065_kprofile_notes_rekey.upgrade(conn)

        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(kprofile_notes)"))).fetchall()]
        assert "printer_id" not in cols
        assert "setting_id" not in cols
        assert "filament_calibration_id" in cols


@pytest.mark.asyncio
async def test_m065_creates_new_unique_index(engine_with_pre_m065):
    async with engine_with_pre_m065.begin() as conn:
        await m065_kprofile_notes_rekey.upgrade(conn)

        idx = (
            await conn.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_kprofile_notes_fc'"))
        ).scalar()
        assert idx is not None
        assert "filament_calibration_id" in idx


@pytest.mark.asyncio
async def test_m065_is_idempotent(engine_with_pre_m065):
    async with engine_with_pre_m065.begin() as conn:
        await m065_kprofile_notes_rekey.upgrade(conn)
        await m065_kprofile_notes_rekey.upgrade(conn)
        # No error → idempotent. Schema still has the new column.
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(kprofile_notes)"))).fetchall()]
        assert "filament_calibration_id" in cols
