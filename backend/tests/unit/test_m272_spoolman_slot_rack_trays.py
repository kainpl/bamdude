"""m272 — a rack AMS (32 / 33) and a direct rack's external inputs (255, 2..8) in ``spoolman_slot_assignments``.

SQLite: the table m053 / m074 left (narrow tray CHECK, AUTOINCREMENT id,
m266's ``spool_registered``) is rebuilt; every row survives, the new slots are
admitted, and the per-unit bound still refuses what no printer has. The
PostgreSQL arm is ``tests/integration/test_m272_postgres.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m272_spoolman_slot_rack_trays import upgrade
from backend.app.models.printer import Printer
from backend.app.schemas.spoolman import slot_tray_in_range

_LEGACY = (
    "CREATE TABLE spoolman_slot_assignments ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE, "
    "ams_id INTEGER NOT NULL CONSTRAINT ck_spoolman_slot_ams_id_range "
    "CHECK ((ams_id >= 0 AND ams_id <= 7) OR (ams_id >= 128 AND ams_id <= 191) OR ams_id = 255), "
    "tray_id INTEGER NOT NULL CONSTRAINT ck_spoolman_slot_tray_id_range CHECK (tray_id >= 0 AND tray_id <= 3), "
    "spoolman_spool_id INTEGER NOT NULL, "
    "assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "spool_registered VARCHAR(40), "
    "CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id)"
    ")"
)


async def _insert(conn, ams_id: int, tray_id: int, spool: int) -> None:
    await conn.execute(
        text(
            "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
            "VALUES (1, :a, :t, :s)"
        ),
        {"a": ams_id, "t": tray_id, "s": spool},
    )


@pytest.mark.asyncio
async def test_rebuild_keeps_rows_and_admits_rack_slots(test_engine):
    sm = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as session:
        session.add(Printer(id=1, name="p", serial_number="M272", ip_address="127.0.0.1", access_code="00000000"))
        await session.commit()
    async with test_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS spoolman_slot_assignments"))
        await conn.execute(text(_LEGACY))
        await _insert(conn, 0, 3, 7)
        await _insert(conn, 130, 0, 8)
        await _insert(conn, 255, 1, 9)
    async with test_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await _insert(conn, 32, 5, 10)  # the old CHECK refuses an AMS 3H slot
        await conn.rollback()

    async with test_engine.begin() as conn:
        await upgrade(conn)
        await upgrade(conn)  # a rerun is a no-op

    async with test_engine.begin() as conn:
        rows = (
            await conn.execute(text("SELECT ams_id, tray_id, spoolman_spool_id FROM spoolman_slot_assignments"))
        ).all()
        assert sorted(rows) == [(0, 3, 7), (130, 0, 8), (255, 1, 9)]
        sql = (
            await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'spoolman_slot_assignments'"))
        ).scalar_one()
        assert "AUTOINCREMENT" in sql.upper()  # the id rule the table had
        await _insert(conn, 32, 8, 11)
        await _insert(conn, 33, 6, 12)
        await _insert(conn, 255, 8, 13)

    for ams_id, tray_id in ((0, 4), (32, 9), (255, 9), (8, 0), (32, -1)):
        async with test_engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await _insert(conn, ams_id, tray_id, 99)
            await conn.rollback()


def test_request_bound_mirrors_the_check():
    for ams_id, tray_id in ((0, 3), (32, 8), (33, 6), (255, 8)):
        slot_tray_in_range(ams_id, tray_id)
    for ams_id, tray_id in ((0, 4), (129, 4), (6, 5)):
        with pytest.raises(ValueError):
            slot_tray_in_range(ams_id, tray_id)
