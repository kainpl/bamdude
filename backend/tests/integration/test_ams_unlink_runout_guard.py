"""An AMS slot that empties MID-PRINT keeps its spool assignment.

The firmware's own empty codes (state 9/10 with cleared content) pass the
``slot_reported_no_filament`` guard by design — the slot really is empty. But
empty means two different things depending on when it is said: while a print
runs it is a runout (the reel is consumed, not removed), while idle it is the
user taking the spool out. The X2D incident (2026-08-23) hit the first case:
the blank tray fell through to the fingerprint compare, "" differed from PETG,
the assignment was unlinked — and the runout row that fired ten minutes later
froze spool=None, leaving the zero close-out with nothing to close.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main as main_module
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


async def _assigned_printer(db_session, printer_factory):
    printer = await printer_factory()
    spool = Spool(color_name="grey", material="PETG", rgba="858585FF", label_weight=1000, weight_used=0)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    assignment = SpoolAssignment(
        spool_id=spool.id,
        printer_id=printer.id,
        ams_id=0,
        tray_id=2,
        fingerprint_color="858585FF",
        fingerprint_type="PETG",
    )
    db_session.add(assignment)
    await db_session.commit()
    return printer, spool


def _emptied_slot_ams():
    # what the stale-tray clearing leaves behind: firmware empty state, blank content
    return [{"id": 0, "tray": [{"id": 2, "state": 9, "tray_type": "", "tray_color": ""}]}]


async def _run_on_ams_change(printer_id, ams_data, printer_state):
    status = SimpleNamespace(state=printer_state, raw_data={})
    with (
        patch.object(main_module.printer_manager, "get_status", return_value=status),
        patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
        patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
        patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
    ):
        await main_module.on_ams_change(printer_id, ams_data)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_empty_slot_mid_print_keeps_the_assignment(db_session, printer_factory, main_db):
    printer, spool = await _assigned_printer(db_session, printer_factory)

    await _run_on_ams_change(printer.id, _emptied_slot_ams(), "RUNNING")

    kept = (
        (await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert [(a.ams_id, a.tray_id, a.spool_id) for a in kept] == [(0, 2, spool.id)]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_empty_slot_while_idle_still_unlinks(db_session, printer_factory, main_db):
    printer, _spool = await _assigned_printer(db_session, printer_factory)

    await _run_on_ams_change(printer.id, _emptied_slot_ams(), "IDLE")

    kept = (
        (await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert kept == []
