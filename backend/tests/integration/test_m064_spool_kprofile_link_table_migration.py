"""m064 test — convert spool_k_profile + spoolman_k_profile to link tables.

Scenarios:
  1. Single row backfills + creates filament_calibration cache row.
  2. Two spool_k_profile rows on different printers → two fc rows + two links.
  3. Two rows on same printer with same K → collapse to one fc, two links.
  4. Row with NULL setting_id → dropped + warning.
  5. spoolman_k_profile same logic.
  6. Idempotent.
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
        calibrated_on_printer_id INTEGER,
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
        # in production — without this, the test passed while real DBs
        # crashed with "no such column: nozzle_diameter" mid-DROP COLUMN.
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
async def test_m064_single_row_creates_fc_and_link(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1')"))
        await conn.execute(
            text(
                "INSERT INTO spool_k_profile "
                "(id, spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES (10, 100, 1, 0, '0.4', 'HS00-0.4', 0.025, 'PETG-HF K=0.025', 3, 'GFSG96')"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)

        # OLD columns gone
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(spool_k_profile)"))).fetchall()]
        for old in ("k_value", "name", "cali_idx", "setting_id", "nozzle_type", "nozzle_diameter"):
            assert old not in cols, f"{old} should be dropped"
        assert "filament_calibration_id" in cols

        # FC row created from the backfill
        fc_rows = (await conn.execute(text("SELECT * FROM filament_calibration"))).mappings().all()
        assert len(fc_rows) == 1
        fc = fc_rows[0]
        assert fc["printer_id"] == 1
        assert fc["filament_id"] == "GFG96"  # GFSG96 → GFG96
        assert fc["pa_k_value"] == 0.025
        assert fc["nozzle_diameter"] == 0.4
        assert fc["nozzle_volume_type"] == "standard"
        assert fc["is_active"] == 0  # m064_backfill creates inactive
        assert fc["source"] == "m064_backfill"

        # Link row points to FC
        link = (await conn.execute(text("SELECT * FROM spool_k_profile WHERE id = 10"))).mappings().first()
        assert link["filament_calibration_id"] == fc["id"]


@pytest.mark.asyncio
async def test_m064_two_printers_create_two_fc_rows(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1'), (2, 'P2')"))
        await conn.execute(
            text(
                "INSERT INTO spool_k_profile "
                "(id, spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES "
                "(11, 100, 1, 0, '0.4', 'HS00-0.4', 0.025, 'r1', 3, 'GFSG96'), "
                "(12, 100, 2, 0, '0.4', 'HS00-0.4', 0.030, 'r2', 5, 'GFSG96')"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)

        fc_count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        assert fc_count == 2
        link_count = (await conn.execute(text("SELECT COUNT(*) FROM spool_k_profile"))).scalar()
        assert link_count == 2


@pytest.mark.asyncio
async def test_m064_same_k_value_collapses_to_one_fc(engine_with_pre_m064):
    """Two spool_k_profile rows on the same printer with same K → one fc row, two links."""
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1')"))
        await conn.execute(
            text(
                "INSERT INTO spool_k_profile "
                "(id, spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES "
                "(13, 100, 1, 0, '0.4', 'HS00-0.4', 0.025, 'r1', 3, 'GFSG96'), "
                "(14, 200, 1, 0, '0.4', 'HS00-0.4', 0.025, 'r2', 3, 'GFSG96')"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)

        fc_count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        assert fc_count == 1
        link_count = (await conn.execute(text("SELECT COUNT(*) FROM spool_k_profile"))).scalar()
        assert link_count == 2

        fc_id = (await conn.execute(text("SELECT id FROM filament_calibration"))).scalar()
        link_fcids = [
            r[0] for r in (await conn.execute(text("SELECT filament_calibration_id FROM spool_k_profile"))).fetchall()
        ]
        assert all(x == fc_id for x in link_fcids)


@pytest.mark.asyncio
async def test_m064_drops_row_with_null_setting_id(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1')"))
        await conn.execute(
            text(
                "INSERT INTO spool_k_profile "
                "(id, spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES (15, 100, 1, 0, '0.4', 'HS00-0.4', 0.025, 'r1', 3, NULL)"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)

        count = (await conn.execute(text("SELECT COUNT(*) FROM spool_k_profile"))).scalar()
        assert count == 0
        fc_count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        assert fc_count == 0


@pytest.mark.asyncio
async def test_m064_spoolman_k_profile_converts_too(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1')"))
        await conn.execute(
            text(
                "INSERT INTO spoolman_k_profile "
                "(id, spoolman_spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES (20, 7, 1, 0, '0.4', 'HS00-0.4', 0.025, 'sm-r1', 3, 'GFSG96')"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)

        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(spoolman_k_profile)"))).fetchall()]
        assert "filament_calibration_id" in cols
        assert "k_value" not in cols
        link = (await conn.execute(text("SELECT * FROM spoolman_k_profile WHERE id = 20"))).mappings().first()
        assert link["filament_calibration_id"] is not None


@pytest.mark.asyncio
async def test_m064_is_idempotent(engine_with_pre_m064):
    async with engine_with_pre_m064.begin() as conn:
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'P1')"))
        await conn.execute(
            text(
                "INSERT INTO spool_k_profile "
                "(id, spool_id, printer_id, extruder, nozzle_diameter, nozzle_type, "
                "k_value, name, cali_idx, setting_id) "
                "VALUES (30, 100, 1, 0, '0.4', 'HS00-0.4', 0.025, 'r1', 3, 'GFSG96')"
            )
        )

        await m064_spool_kprofile_link_table.upgrade(conn)
        await m064_spool_kprofile_link_table.upgrade(conn)

        fc_count = (await conn.execute(text("SELECT COUNT(*) FROM filament_calibration"))).scalar()
        link_count = (await conn.execute(text("SELECT COUNT(*) FROM spool_k_profile"))).scalar()
        assert fc_count == 1
        assert link_count == 1
