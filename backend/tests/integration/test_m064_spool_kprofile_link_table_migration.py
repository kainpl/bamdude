"""m064 test — convert spool_k_profile + spoolman_k_profile to link tables.

Scenarios:
  1. Existing rows are dropped (clean-slate; printer push repopulates
     filament_calibration; user re-links via PA tab).
  2. OLD K-data columns dropped, filament_calibration_id FK added.
  3. spoolman_k_profile inline UNIQUE constraint on nozzle_diameter is
     replaced by the new one on filament_calibration_id.
  4. Idempotent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m064_spool_kprofile_link_table


def _post_m063_filament_calibration_ddl() -> str:
    return """
    CREATE TABLE filament_calibration (
        id INTEGER PRIMARY KEY,
        printer_id INTEGER NOT NULL,
        filament_id TEXT NOT NULL,
        filament_setting_id TEXT,
        nozzle_diameter REAL NOT NULL,
        nozzle_volume_type TEXT NOT NULL,
        extruder_id INTEGER NOT NULL DEFAULT 0,
        pa_k_value REAL, pa_n_coef REAL,
        flow_ratio REAL, confidence INTEGER,
        cali_mode TEXT NOT NULL, source TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        cali_idx INTEGER,
        name TEXT NOT NULL,
        notes TEXT,
        nozzle_id TEXT,
        calibrated_by_user_id INTEGER,
        created_at TEXT
    )
    """


def _pre_m064_spool_kprofile_ddl(table: str, extra_unique: str = "") -> str:
    return f"""
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY,
        spool_id INTEGER,
        spoolman_spool_id INTEGER,
        printer_id INTEGER NOT NULL,
        extruder INTEGER NOT NULL DEFAULT 0,
        nozzle_diameter TEXT NOT NULL DEFAULT '0.4',
        nozzle_type TEXT,
        k_value REAL,
        name TEXT,
        cali_idx INTEGER,
        setting_id TEXT,
        created_at TEXT
        {extra_unique}
    )
    """


@pytest_asyncio.fixture
async def engine_with_pre_m064():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("CREATE TABLE spool (id INTEGER PRIMARY KEY, slicer_filament TEXT)"))
        await conn.execute(text(_post_m063_filament_calibration_ddl()))
        await conn.execute(text(_pre_m064_spool_kprofile_ddl("spool_k_profile")))
        # spoolman_k_profile ships with an inline UNIQUE constraint that
        # references nozzle_diameter (m053). Reproduce it so the migration's
        # column-drop path has to deal with the same blocker SQLite raises
        # in production.
        await conn.execute(
            text(
                _pre_m064_spool_kprofile_ddl(
                    "spoolman_k_profile",
                    extra_unique=", CONSTRAINT uq_spoolman_kp UNIQUE (spoolman_spool_id, printer_id, extruder, nozzle_diameter)",
                )
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_m064_clears_existing_rows(engine_with_pre_m064):
    """All pre-existing rows are dropped — fresh repopulate path."""
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1')"))
        await conn.execute(
            text(
                "INSERT INTO spool_k_profile "
                "(id, spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES (10, 100, 1, 0, '0.4', 'HS00-0.4', 0.025, 'old', 3, 'GFSG96')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO spoolman_k_profile "
                "(id, spoolman_spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES (20, 7, 1, 0, '0.4', 'HS00-0.4', 0.025, 'old', 3, 'GFSG96')"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)

        # Both tables emptied
        assert (await conn.execute(text("SELECT COUNT(*) FROM spool_k_profile"))).scalar() == 0
        assert (await conn.execute(text("SELECT COUNT(*) FROM spoolman_k_profile"))).scalar() == 0
        # No filament_calibration auto-created — the printer push will fill it.
        assert (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar() == 0


@pytest.mark.asyncio
async def test_m064_drops_old_columns_adds_fk_column(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await m064_spool_kprofile_link_table.upgrade(conn)

        for table in ("spool_k_profile", "spoolman_k_profile"):
            cols = [r[1] for r in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()]
            for old in ("k_value", "name", "cali_idx", "setting_id", "nozzle_type", "nozzle_diameter"):
                assert old not in cols, f"{old} should be dropped from {table}"
            assert "filament_calibration_id" in cols


@pytest.mark.asyncio
async def test_m064_spoolman_unique_constraint_replaced(engine_with_pre_m064):
    """Pre-m064 UNIQUE was (..., nozzle_diameter). Post-m064 it must reference
    filament_calibration_id instead — checked via sqlite_master."""
    async with engine_with_pre_m064.begin() as conn:
        await m064_spool_kprofile_link_table.upgrade(conn)

        # sqlite_autoindex / explicit index — look for the named constraint
        idx_sql = (
            (
                await conn.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type IN ('index','table') "
                        "AND tbl_name='spoolman_k_profile'"
                    )
                )
            )
            .scalars()
            .all()
        )
        joined = " ".join(s or "" for s in idx_sql)
        assert "filament_calibration_id" in joined
        # And the OLD reference is gone
        assert "nozzle_diameter" not in joined


@pytest.mark.asyncio
async def test_m064_is_idempotent(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await m064_spool_kprofile_link_table.upgrade(conn)
        # Re-run should be a no-op
        await m064_spool_kprofile_link_table.upgrade(conn)
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(spool_k_profile)"))).fetchall()]
        assert "filament_calibration_id" in cols
