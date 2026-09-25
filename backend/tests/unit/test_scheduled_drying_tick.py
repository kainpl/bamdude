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
    sd._seen_active = set()
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


# ------------------------------------------------ review fixes (whole-branch review)


def _no_report():
    """A printer whose client exists but has reported nothing: the state before the first push."""
    return SimpleNamespace(state="IDLE", firmware_version=None, raw_data={})


@pytest.mark.asyncio
async def test_an_unreachable_printer_waits_instead_of_failing(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    pm.is_connected.return_value = False
    pm.get_status.return_value = _no_report()
    row = await _run(db_session, printer)
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert (row.status, row.reason) == ("pending", "printer_offline")
    pm.notifier.on_scheduled_drying_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_printer_that_has_not_reported_yet_waits(db_session, printer_factory, pm):
    """The first seconds after a (re)connect: connected, no firmware, no AMS yet."""
    printer = await printer_factory(model="X1C")
    pm.get_status.return_value = _no_report()
    row = await _run(db_session, printer)
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert (row.status, row.reason) == ("pending", "printer_offline")
    pm.send_drying_command.assert_not_called()


@pytest.mark.asyncio
async def test_a_running_run_survives_a_printer_that_has_not_reported(db_session, printer_factory, pm):
    """Not reported is not "not drying": the run and the queue hold stay."""
    printer = await printer_factory(model="X1C")
    pm.is_connected.return_value = False
    pm.get_status.return_value = _no_report()
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(hours=1))
    holds = {printer.id: 1.0}
    await sd.tick(db_session, now=NOW, drying_in_progress=holds, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.status == "running"
    assert printer.id in holds


@pytest.mark.asyncio
async def test_a_print_started_elsewhere_takes_the_cycle_back(db_session, printer_factory, pm):
    """Print now, the printer's screen, the slicer: any print, not only a queue dispatch."""
    printer = await printer_factory(model="X1C")
    pm.get_status.return_value = SimpleNamespace(
        state="RUNNING", firmware_version="01.11.00.00", raw_data={"ams": [_unit(dry_time=300)]}
    )
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(hours=1))
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert (row.status, row.started_at) == ("pending", None)
    pm.send_drying_command.assert_called_with(printer.id, 0, 0, 0, mode=0)


@pytest.mark.asyncio
async def test_a_cycle_that_never_started_is_a_reported_failure(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(minutes=5))
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert (row.status, row.reason) == ("failed", "not_started")
    pm.notifier.on_scheduled_drying_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_cycle_seen_drying_then_stopped_early_is_a_silent_cancel(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    row = await _run(db_session, printer, status="running", started_at=NOW - timedelta(minutes=5))
    pm.get_status.return_value = SimpleNamespace(
        state="IDLE", firmware_version="01.11.00.00", raw_data={"ams": [_unit(dry_time=400)]}
    )
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    pm.get_status.return_value = SimpleNamespace(
        state="IDLE", firmware_version="01.11.00.00", raw_data={"ams": [_unit()]}
    )
    await sd.tick(db_session, now=NOW + timedelta(minutes=1), drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.status == "cancelled"
    pm.notifier.on_scheduled_drying_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_printing_printer_waits_as_busy_whatever_the_ams_reports(db_session, printer_factory, pm):
    printer = await printer_factory(model="X1C")
    pm.get_status.return_value = SimpleNamespace(
        state="RUNNING", firmware_version="01.11.00.00", raw_data={"ams": [_unit(reasons=[0])]}
    )
    row = await _run(db_session, printer)
    await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.refresh(row)
    assert row.reason == "printer_busy"


@pytest.mark.asyncio
async def test_a_started_run_is_committed_before_the_pass_goes_on(db_session, printer_factory, pm):
    """A later failure in the same pass must not roll back a cycle the printer is already running."""
    first = await printer_factory(model="X1C")
    second = await printer_factory(model="X1C")
    started = await _run(db_session, first, start_after=NOW - timedelta(minutes=2))
    await _run(db_session, second, start_after=NOW - timedelta(minutes=1))
    healthy = pm.get_status.return_value

    def status(pid):
        if pid == second.id:
            raise RuntimeError("database is locked")
        return healthy

    pm.get_status.side_effect = status
    with pytest.raises(RuntimeError):
        await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    await db_session.rollback()
    await db_session.refresh(started)
    assert started.status == "running"


@pytest.mark.asyncio
async def test_the_tick_says_what_it_did(db_session, printer_factory, pm, caplog):
    printer = await printer_factory(model="X1C")
    await _run(db_session, printer)
    with caplog.at_level("INFO", logger="backend.app.services.scheduled_drying"):
        await sd.tick(db_session, now=NOW, drying_in_progress={}, dispatching_printers=set())
    assert any("started" in r.getMessage() and f"printer {printer.id}" in r.getMessage() for r in caplog.records)
