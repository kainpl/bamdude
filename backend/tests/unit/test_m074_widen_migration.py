"""Migration tests for m074 — verify the SQLite table-rebuild path actually
widens the constraint and preserves row data.

The widened CHECK on the model already ships via ``Base.metadata.create_all``
for fresh installs (see ``test_spoolman_slot_ams_id_range_widen``). This
file simulates the legacy state where the constraint is still narrow and
asserts m074's ``upgrade(conn)`` brings it to parity.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m074_spoolman_slot_ams_id_range_widen import upgrade
from backend.app.models.printer import Printer


async def _seed_printer(test_engine, *, pid: int, serial: str) -> None:
    sm = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as session:
        session.add(Printer(id=pid, name="p", serial_number=serial, ip_address="127.0.0.1", access_code="00000000"))
        await session.commit()


@pytest.mark.asyncio
async def test_widen_rebuilds_sqlite_table_and_preserves_rows(test_engine):
    """Start with the legacy narrow CHECK + existing rows, run upgrade,
    confirm AMS-HT inserts now succeed and the legacy rows still exist."""

    await _seed_printer(test_engine, pid=1, serial="MTEST-1")

    async with test_engine.begin() as conn:
        # Replace the widened-by-create_all table with the legacy narrow form.
        await conn.execute(text("DROP TABLE IF EXISTS spoolman_slot_assignments"))
        await conn.execute(
            text(
                "CREATE TABLE spoolman_slot_assignments ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE, "
                "ams_id INTEGER NOT NULL CONSTRAINT ck_spoolman_slot_ams_id_range "
                "  CHECK ((ams_id >= 0 AND ams_id <= 7) OR ams_id = 255), "
                "tray_id INTEGER NOT NULL CONSTRAINT ck_spoolman_slot_tray_id_range "
                "  CHECK (tray_id >= 0 AND tray_id <= 3), "
                "spoolman_spool_id INTEGER NOT NULL, "
                "assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "CONSTRAINT uq_spoolman_slot_assignment UNIQUE (printer_id, ams_id, tray_id)"
                ")"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                "VALUES (1, 0, 0, 7)"
            )
        )

    # Sanity: pre-upgrade AMS-HT insert is rejected.
    async with test_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                    "VALUES (1, 128, 0, 99)"
                )
            )

    # Apply m074.
    async with test_engine.begin() as conn:
        await upgrade(conn)

    # Post-upgrade: AMS-HT insert succeeds, legacy row still there.
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                "VALUES (1, 128, 0, 99)"
            )
        )
        legacy = (
            await conn.execute(text("SELECT spoolman_spool_id FROM spoolman_slot_assignments WHERE ams_id = 0"))
        ).scalar_one()
        assert legacy == 7
        ht = (
            await conn.execute(text("SELECT spoolman_spool_id FROM spoolman_slot_assignments WHERE ams_id = 128"))
        ).scalar_one()
        assert ht == 99

    # Still-narrow rejection holds (100 is not in any of the three accepted bands).
    async with test_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                    "VALUES (1, 100, 0, 1)"
                )
            )


@pytest.mark.asyncio
async def test_widen_is_idempotent_on_already_widened_table(test_engine):
    """A second invocation on an install that already has the widened
    formula must be a no-op (no rebuild, no row loss)."""
    await _seed_printer(test_engine, pid=1, serial="MTEST-2")

    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO spoolman_slot_assignments (printer_id, ams_id, tray_id, spoolman_spool_id) "
                "VALUES (1, 128, 0, 123)"
            )
        )

    async with test_engine.begin() as conn:
        await upgrade(conn)

    async with test_engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT spoolman_spool_id FROM spoolman_slot_assignments WHERE ams_id = 128"))
        ).scalar_one()
        assert row == 123
