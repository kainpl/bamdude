"""tick(): materialise rules, start due runs, follow running ones, release the printer (spec §Планувальник)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying
from backend.app.services import scheduled_drying as sd

NOW = datetime(2026, 9, 25, 22, 0)  # naive UTC = 01:00 Kyiv


@pytest.fixture(autouse=True)
def _fresh_module_state():
    sd._last_running = set()
    sd._last_prune = None
    yield


def _unit(dry_time=0, dry_status=0, reasons=()):
    return {
        "id": 0,
        "module_type": "n3f",
        "tray": [{"tray_type": "PLA"}],
        "dry_time": dry_time,
        "dry_status": dry_status,
        "dry_sf_reason": list(reasons),
    }


@pytest.fixture
def pm():
    mock = MagicMock()
    mock.is_connected.return_value = True
    mock.get_status.return_value = SimpleNamespace(
        state="IDLE", firmware_version="01.11.00.00", raw_data={"ams": [_unit()]}
    )
    mock.send_drying_command.return_value = True
    notifier = MagicMock()
    for name in ("on_scheduled_drying_started", "on_scheduled_drying_completed", "on_scheduled_drying_failed"):
        setattr(notifier, name, AsyncMock())
    with (
        patch.object(sd, "printer_manager", mock),
        patch.object(sd, "notification_service", notifier),
        patch.object(sd, "server_timezone", lambda: ZoneInfo("Europe/Kyiv")),
    ):
        mock.notifier = notifier
        yield mock


async def _run(db, printer, **kw):
    row = ScheduledDrying(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, **kw)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_a_due_run_starts_and_holds_the_printer(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer, start_after=NOW - timedelta(minutes=1))
    holds: dict[int, float] = {}

    result = await sd.tick(db_session, now=NOW, drying_in_progress=holds, dispatching_printers=set())

    await db_session.refresh(row)
    assert row.status == "running"
    assert printer.id in holds and printer.id in result.running_printers
    assert (printer.id, 0) in result.reserved_units
    pm.send_drying_command.assert_called_once()
    pm.notifier.on_scheduled_drying_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_busy_printer_waits(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    pm.get_status.return_value.state = "RUNNING"
    row = await _run(db_session, printer)
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert (row.status, row.reason) == ("pending", "printer_busy")


@pytest.mark.asyncio
async def test_a_printer_dispatched_this_pass_waits(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer)
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers={printer.id})
    await db_session.refresh(row)
    assert row.reason == "printer_busy"


@pytest.mark.asyncio
async def test_the_blocker_is_the_waiting_reason(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    pm.get_status.return_value.raw_data = {"ams": [_unit(reasons=[3, 8])]}
    row = await _run(db_session, printer)
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.reason == "blocked_power"


@pytest.mark.asyncio
async def test_a_passed_window_is_skipped_and_reported(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(
        db_session,
        printer,
        start_after=NOW - timedelta(hours=3),
        latest_start=NOW - timedelta(hours=1),
        reason="printer_busy",
    )
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert (row.status, row.reason) == ("skipped", "window_passed")
    pm.notifier.on_scheduled_drying_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_rule_materialises_its_next_run(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    # Updated a day ago: last night's 02:00-04:00 window has closed and is passed
    # over silently; the next run is tonight's.
    rule = DryingSchedule(
        printer_id=printer.id,
        ams_id=0,
        temp=55,
        duration_hours=8,
        start_time="02:00",
        weekdays=127,
        latest_start="04:00",
        updated_at=NOW - timedelta(days=1),
    )
    db_session.add(rule)
    await db_session.commit()

    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())

    run = (await db_session.execute(select(ScheduledDrying))).scalar_one()
    assert (run.schedule_id, run.start_after, run.latest_start) == (
        rule.id,
        datetime(2026, 9, 25, 23, 0),
        datetime(2026, 9, 26, 1, 0),
    )
    pm.notifier.on_scheduled_drying_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_finished_cycle_completes_and_releases_the_printer(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(hours=8))
    holds = {printer.id: 1.0}
    await sd.tick(db_session, now=NOW, drying_in_progress=holds, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.status == "completed"
    assert printer.id not in holds
    pm.notifier.on_scheduled_drying_completed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_cooling_cycle_is_still_running(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    pm.get_status.return_value.raw_data = {"ams": [_unit(dry_time=0, dry_status=3)]}
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(hours=8))
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.status == "running"


@pytest.mark.asyncio
async def test_an_early_stop_on_an_idle_printer_is_a_silent_cancel(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(hours=1))
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.status == "cancelled"
    pm.notifier.on_scheduled_drying_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_preempt_puts_a_running_run_back_to_pending(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(hours=1))
    assert await sd.preempt_for_print(db_session, printer.id, NOW) == 1
    await db_session.refresh(row)
    assert (row.status, row.reason, row.started_at) == ("pending", "interrupted", None)
    pm.send_drying_command.assert_called_with(printer.id, 0, 0, 0, mode=0)
