"""Smoke test for m062 — filament_calibration + calibration_session +
calibration_audit tables + index + partial unique + flag columns on
print_archives + print_queue.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m062_filament_calibration


@pytest_asyncio.fixture
async def engine_with_prereqs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
        # m062 adds columns to these, so they must exist beforehand
        await conn.execute(text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY, file_path TEXT)"))
        await conn.execute(text("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, status TEXT)"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def _table_exists(conn, name: str) -> bool:
    r = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    )
    return r.scalar() is not None


async def _index_exists(conn, name: str) -> bool:
    r = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": name},
    )
    return r.scalar() is not None


async def _columns(conn, table: str) -> set[str]:
    rows = (await conn.execute(text(f"PRAGMA table_info('{table}')"))).fetchall()
    return {r[1] for r in rows}


@pytest.mark.asyncio
async def test_m062_creates_filament_calibration(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)
        assert await _table_exists(conn, "filament_calibration")
        cols = await _columns(conn, "filament_calibration")
        expected = {
            "id",
            "printer_model",
            "filament_id",
            "filament_setting_id",
            "nozzle_diameter",
            "nozzle_volume_type",
            "extruder_id",
            "pa_k_value",
            "pa_n_coef",
            "flow_ratio",
            "confidence",
            "cali_mode",
            "source",
            "is_active",
            "cali_idx",
            "name",
            "notes",
            "nozzle_id",
            "calibrated_on_printer_id",
            "calibrated_by_user_id",
            "created_at",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"
        assert await _index_exists(conn, "ix_filament_cali_lookup")
        assert await _index_exists(conn, "ux_filament_cali_active")


@pytest.mark.asyncio
async def test_m062_creates_calibration_session(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)
        assert await _table_exists(conn, "calibration_session")
        cols = await _columns(conn, "calibration_session")
        expected = {
            "id",
            "printer_id",
            "user_id",
            "cali_mode",
            "method",
            "nozzle_diameter",
            "nozzle_volume_type",
            "extruder_id",
            "filaments_json",
            "status",
            "mqtt_sequence_id",
            "print_queue_item_id",
            "parent_session_id",
            "stage",
            "coarse_ratio",
            "error_message",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"
        assert await _index_exists(conn, "ix_calibration_session_printer")


@pytest.mark.asyncio
async def test_m062_creates_calibration_audit(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)
        assert await _table_exists(conn, "calibration_audit")
        cols = await _columns(conn, "calibration_audit")
        expected = {
            "id",
            "printer_id",
            "session_id",
            "filament_calibration_id",
            "user_id",
            "action",
            "payload_json",
            "sequence_id",
            "result",
            "error_message",
            "created_at",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"


@pytest.mark.asyncio
async def test_m062_adds_print_archives_flags(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)
        cols = await _columns(conn, "print_archives")
        assert "is_calibration" in cols
        assert "calibration_session_id" in cols


@pytest.mark.asyncio
async def test_m062_adds_print_queue_flags(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)
        cols = await _columns(conn, "print_queue")
        assert "is_calibration" in cols
        assert "calibration_session_id" in cols


@pytest.mark.asyncio
async def test_m062_partial_unique_enforces_one_active(engine_with_prereqs):
    """Two is_active=True rows for same combo must fail."""
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)

    # Insert first row in a fresh transaction
    async with engine_with_prereqs.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO filament_calibration "
                "(printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id, "
                " cali_mode, source, is_active, name, created_at) "
                "VALUES ('P1S','GFG00',0.4,'standard',0,'pa_line','manual',1,'r1',CURRENT_TIMESTAMP)"
            )
        )

    # Inserting another active row with same combo must fail
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    with pytest.raises(IntegrityError):
        async with engine_with_prereqs.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO filament_calibration "
                    "(printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id, "
                    " cali_mode, source, is_active, name, created_at) "
                    "VALUES ('P1S','GFG00',0.4,'standard',0,'pa_line','manual',1,'r2',CURRENT_TIMESTAMP)"
                )
            )


@pytest.mark.asyncio
async def test_m062_is_idempotent(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m062_filament_calibration.upgrade(conn)
        await m062_filament_calibration.upgrade(conn)
        # Still exists, no errors
        assert await _table_exists(conn, "filament_calibration")
        assert await _table_exists(conn, "calibration_session")
        assert await _table_exists(conn, "calibration_audit")
