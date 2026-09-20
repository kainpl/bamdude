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
        # The deferred overlay rebuild is the first thing this callback does and
        # it REPLACES the printer's map. These tests are about the unlink rule,
        # so the map they set up must survive to be read.
        patch(
            "backend.app.services.ams_backup_compatibility_apply.rebuild_once",
            new=AsyncMock(),
        ),
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


# --- The advertised profile is not a stranger's spool -------------------------
#
# Under colour mode the printer echoes the CANONICAL colour, which matches
# neither the fingerprint (snapshotted before the publish) nor the spool. Left
# alone, the very first AMS push after a publish unlinked the assignment and
# forgot the overlay, so routing read the masked slot as black: the feature
# destroyed itself. The overlay's own ``matches_live`` is the proof it was us.


async def _red_spool_on_a_black_tray(db_session, printer_factory):
    printer = await printer_factory()
    spool = Spool(color_name="red", material="PETG", rgba="FF0000FF", label_weight=1000, weight_used=0)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=0,
            tray_id=2,
            fingerprint_color="FF0000FF",
            fingerprint_type="PETG",
        )
    )
    await db_session.commit()
    return printer, spool


def _live_tray(color: str):
    return [
        {"id": 0, "tray": [{"id": 2, "state": 11, "tray_type": "PETG", "tray_color": color, "tray_info_idx": "GFG99"}]}
    ]


def _advertised_black(printer_id: int) -> None:
    from backend.app.services import ams_advertised_overlay as overlay
    from backend.app.services.ams_advertised_overlay import OverlayEntry

    overlay.replace_printer(
        printer_id, {(0, 2): OverlayEntry("PETG", "FF0000FF", "GFG99", (), "000000FF", "GFG99", "internal")}
    )


async def _assignments(db_session, printer_id):
    return (
        (await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer_id)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_tray_reconfigured_to_what_we_advertised_keeps_its_assignment(db_session, printer_factory, main_db):
    printer, spool = await _red_spool_on_a_black_tray(db_session, printer_factory)
    _advertised_black(printer.id)

    await _run_on_ams_change(printer.id, _live_tray("000000FF"), "IDLE")

    kept = await _assignments(db_session, printer.id)
    assert [(a.tray_id, a.spool_id) for a in kept] == [(2, spool.id)]
    # The fingerprint follows what the slot now shows, or the same push would
    # ask the same question forever.
    assert (kept[0].fingerprint_color, kept[0].fingerprint_type) == ("000000FF", "PETG")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_black_tray_we_never_advertised_still_unlinks(db_session, printer_factory, main_db):
    printer, _spool = await _red_spool_on_a_black_tray(db_session, printer_factory)

    await _run_on_ams_change(printer.id, _live_tray("000000FF"), "IDLE")

    assert await _assignments(db_session, printer.id) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_third_colour_unlinks_even_with_an_entry_on_the_slot(db_session, printer_factory, main_db):
    """The entry says black; the slot shows green. Somebody reconfigured it
    from the printer's screen, and that is a spool change like any other."""
    printer, _spool = await _red_spool_on_a_black_tray(db_session, printer_factory)
    _advertised_black(printer.id)

    await _run_on_ams_change(printer.id, _live_tray("00FF00FF"), "IDLE")

    assert await _assignments(db_session, printer.id) == []
