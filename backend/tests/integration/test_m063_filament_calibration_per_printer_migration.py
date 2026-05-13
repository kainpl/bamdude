"""m063 test — verify printer_model → printer_id transition.

Three scenarios:
  1. Existing rows with calibrated_on_printer_id set → backfilled to printer_id.
  2. Orphan rows (no calibrated_on_printer_id) → dropped with warning.
  3. Two rows for same combo on different printers → both survive under new
     unique index (which is keyed on printer_id, not printer_model).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m063_filament_calibration_per_printer


@pytest_asyncio.fixture
async def engine_with_pre_m063():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, model TEXT, name TEXT)"))
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        # filament_calibration as it lands after m062
        await conn.execute(
            text(
                """
                CREATE TABLE filament_calibration (
                    id INTEGER PRIMARY KEY,
                    printer_model TEXT NOT NULL,
                    filament_id TEXT NOT NULL,
                    filament_setting_id TEXT,
                    nozzle_diameter REAL NOT NULL,
                    nozzle_volume_type TEXT NOT NULL,
                    extruder_id INTEGER NOT NULL DEFAULT 0,
                    pa_k_value REAL, pa_n_coef REAL,
                    flow_ratio REAL, confidence INTEGER,
                    cali_mode TEXT NOT NULL, source TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    cali_idx INTEGER, name TEXT NOT NULL, notes TEXT,
                    calibrated_on_printer_id INTEGER,
                    calibrated_by_user_id INTEGER,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ux_filament_cali_active "
                "ON filament_calibration "
                "(printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id) "
                "WHERE is_active = 1"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_m063_backfills_printer_id_from_calibrated_on_printer_id(engine_with_pre_m063):
    async with engine_with_pre_m063.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, model, name) VALUES (1, 'X1C', 'P1')"))
        await conn.execute(
            text(
                """
                INSERT INTO filament_calibration
                    (printer_model, filament_id, nozzle_diameter, nozzle_volume_type,
                     extruder_id, pa_k_value, cali_mode, source, name,
                     calibrated_on_printer_id)
                VALUES ('X1C', 'GFG96', 0.4, 'standard', 0, 0.025, 'pa_line',
                        'manual', 'PETG-HF K=0.025', 1)
                """
            )
        )

        await m063_filament_calibration_per_printer.upgrade(conn)

        # printer_id populated, printer_model + calibrated_on_printer_id dropped
        rows = (await conn.execute(text("SELECT printer_id, pa_k_value FROM filament_calibration"))).mappings().all()
        assert len(rows) == 1
        assert rows[0]["printer_id"] == 1

        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(filament_calibration)"))).fetchall()]
        assert "printer_model" not in cols
        assert "calibrated_on_printer_id" not in cols
        assert "printer_id" in cols
        assert "calibrated_by_user_id" in cols  # kept
        assert "nozzle_id" in cols  # added by m063 when missing from legacy m062 shape


@pytest.mark.asyncio
async def test_m063_drops_orphans_with_no_calibrated_on_printer_id(engine_with_pre_m063):
    async with engine_with_pre_m063.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO filament_calibration
                    (printer_model, filament_id, nozzle_diameter, nozzle_volume_type,
                     extruder_id, pa_k_value, cali_mode, source, name)
                VALUES ('X1C', 'GFG96', 0.4, 'standard', 0, 0.025, 'pa_line', 'manual', 'orphan')
                """
            )
        )

        await m063_filament_calibration_per_printer.upgrade(conn)

        count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        assert count == 0


@pytest.mark.asyncio
async def test_m063_new_unique_index_uses_printer_id(engine_with_pre_m063):
    """Two rows with same combo on different printers must coexist under
    the new index (printer_model→printer_id transition).

    Test setup drops the OLD partial unique index first so we can stage
    two rows that would otherwise violate it — in real upgrades the OLD
    index would already be there with at most one active row per combo;
    after m063 the relaxed-on-printer_id index lets the second row become
    active without violating uniqueness. Here we just verify the index
    contents post-migration.
    """
    async with engine_with_pre_m063.begin() as conn:
        # Drop old constraint to stage the test data
        await conn.execute(text("DROP INDEX ux_filament_cali_active"))

        await conn.execute(text("INSERT INTO printers (id, model, name) VALUES (1, 'X1C', 'P1')"))
        await conn.execute(text("INSERT INTO printers (id, model, name) VALUES (2, 'X1C', 'P2')"))
        await conn.execute(
            text(
                """
                INSERT INTO filament_calibration
                    (printer_model, filament_id, nozzle_diameter, nozzle_volume_type,
                     extruder_id, pa_k_value, cali_mode, source, name,
                     calibrated_on_printer_id, is_active)
                VALUES
                    ('X1C', 'GFG96', 0.4, 'standard', 0, 0.025, 'pa_line', 'manual', 'P1 calib', 1, 1),
                    ('X1C', 'GFG96', 0.4, 'standard', 0, 0.030, 'pa_line', 'manual', 'P2 calib', 2, 1)
                """
            )
        )

        await m063_filament_calibration_per_printer.upgrade(conn)

        count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        assert count == 2

        # New unique index exists and references printer_id
        idx = (
            await conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_filament_cali_active'")
            )
        ).scalar()
        assert idx and "printer_id" in idx


@pytest.mark.asyncio
async def test_m063_is_idempotent(engine_with_pre_m063):
    """Running m063 twice should not error."""
    async with engine_with_pre_m063.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, model, name) VALUES (1, 'X1C', 'P1')"))
        await conn.execute(
            text(
                """
                INSERT INTO filament_calibration
                    (printer_model, filament_id, nozzle_diameter, nozzle_volume_type,
                     extruder_id, pa_k_value, cali_mode, source, name,
                     calibrated_on_printer_id)
                VALUES ('X1C', 'GFG96', 0.4, 'standard', 0, 0.025, 'pa_line',
                        'manual', 'PETG K', 1)
                """
            )
        )
        await m063_filament_calibration_per_printer.upgrade(conn)
        # Re-run — should be no-op
        await m063_filament_calibration_per_printer.upgrade(conn)
        count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        assert count == 1
