"""Regression tests for D.4: ``spoolman_slot_assignments.ck_spoolman_slot_ams_id_range``
admits AMS-HT (ams_id 128..191) and external (255) alongside the legacy 0..7.

Upstream Bambuddy #1274 / commit ``af52c4f2``. H2C / H2D AMS-HT units
report ``ams_id`` in the 128+ range; the original m053 constraint
``(ams_id >= 0 AND ams_id <= 7) OR ams_id = 255`` rejected every
AMS-HT slot link.

Uses the project ``db_session`` fixture (in-memory SQLite created with
the *current* model schema via ``Base.metadata.create_all``). The
model-level CHECK is what ships to fresh installs; the m074 migration
brings existing installs to parity.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.models.printer import Printer
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment


async def _seed_printer(db_session) -> None:
    db_session.add(
        Printer(
            id=1,
            name="p",
            serial_number="TEST-SERIAL-1",
            ip_address="127.0.0.1",
            access_code="00000000",
        )
    )
    await db_session.commit()


class TestAmsIdRangeAccept:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ams_id", [0, 3, 7, 128, 135, 191, 255])
    async def test_admitted_ams_ids(self, db_session, ams_id):
        """0..7 (standard AMS), 128..191 (AMS-HT), 255 (external) all admitted."""
        await _seed_printer(db_session)
        db_session.add(
            SpoolmanSlotAssignment(
                printer_id=1,
                ams_id=ams_id,
                tray_id=0,
                spoolman_spool_id=ams_id + 1000,
            )
        )
        await db_session.commit()
        rows = (
            (await db_session.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.ams_id == ams_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


class TestAmsIdRangeReject:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ams_id", [-1, 8, 100, 127, 192, 254, 256])
    async def test_rejected_ams_ids(self, db_session, ams_id):
        """Values outside (0..7, 128..191, 255) must still be rejected."""
        await _seed_printer(db_session)
        db_session.add(
            SpoolmanSlotAssignment(
                printer_id=1,
                ams_id=ams_id,
                tray_id=0,
                spoolman_spool_id=ams_id + 2000,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


class TestAmsHtRealWorldScenario:
    @pytest.mark.asyncio
    async def test_h2d_left_nozzle_ams_ht_link(self, db_session):
        """The reporter's exact scenario from #1274 — H2D with AMS-HT on the
        left nozzle reports ``ams_id=128, tray_id=0`` for its single slot."""
        await _seed_printer(db_session)
        db_session.add(SpoolmanSlotAssignment(printer_id=1, ams_id=128, tray_id=0, spoolman_spool_id=42))
        await db_session.commit()
        row = (
            await db_session.execute(
                select(SpoolmanSlotAssignment).where(
                    SpoolmanSlotAssignment.printer_id == 1,
                    SpoolmanSlotAssignment.ams_id == 128,
                )
            )
        ).scalar_one()
        assert row.spoolman_spool_id == 42
