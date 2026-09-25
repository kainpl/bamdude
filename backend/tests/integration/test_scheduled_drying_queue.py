"""Scheduled drying through the real ``check_queue`` pass (spec §Тестування, plan Review Focus 1, 4, 5).

A running scheduled cycle holds its printer when ``queue_drying_block`` is on,
yields to the print when it is off — on every model, including one that dries
through a print — keeps holding through its cooling tail, is restored on the
first pass after a restart, and nothing it does can stop the queue.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.scheduled_drying import ScheduledDrying
from backend.app.models.settings import Settings
from backend.app.services import scheduled_drying as sd
from backend.app.services.print_scheduler import PrintScheduler


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _fresh_module_state():
    sd._last_running = set()
    sd._last_prune = None
    sd._seen_active = set()
    yield


@pytest.fixture
def scheduler(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", _session_ctx)
    return PrintScheduler()


def _unit(dry_time=0, dry_status=0):
    return {
        "id": 0,
        "module_type": "n3f",
        "tray": [{"tray_type": "PLA"}],
        "dry_time": dry_time,
        "dry_status": dry_status,
        "dry_sf_reason": [],
    }


@pytest.fixture
def farm():
    """One fake printer manager for the scheduler and the drying writer alike."""
    state = SimpleNamespace(
        state="IDLE",
        firmware_version="01.11.00.00",
        raw_data={"ams": [_unit(dry_time=300)]},
        subtask_id=None,
        gcode_file=None,
    )
    pm = MagicMock()
    pm.is_connected.return_value = True
    pm.get_status.return_value = state
    pm.is_awaiting_plate_clear.return_value = False
    pm.get_model.return_value = "X1C"
    pm.send_drying_command.return_value = True
    notifier = MagicMock()
    for name in ("on_scheduled_drying_started", "on_scheduled_drying_completed", "on_scheduled_drying_failed"):
        setattr(notifier, name, AsyncMock())
    with (
        patch("backend.app.services.print_scheduler.printer_manager", pm),
        patch.object(sd, "printer_manager", pm),
        patch.object(sd, "notification_service", notifier),
        patch.object(sd, "server_timezone", lambda: ZoneInfo("Europe/Kyiv")),
    ):
        pm.state = state
        yield pm


async def _setting(db, key, value):
    db.add(Settings(key=key, value=value))
    await db.commit()


async def _printer_with_work(db_session, printer_factory, raw_gcode_source, model="X1C"):
    printer = await printer_factory(model=model)
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()
    item = PrintQueueItem(queue_id=printer.id, status="pending", position=1, library_file_id=raw_gcode_source.id)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return printer, item


async def _running_run(db_session, printer):
    run = ScheduledDrying(
        printer_id=printer.id,
        ams_id=0,
        temp=55,
        duration_hours=8,
        status="running",
        started_at=_now() - timedelta(hours=1),
    )
    db_session.add(run)
    await db_session.commit()
    return run


@pytest.mark.asyncio
@pytest.mark.integration
async def test_with_the_block_on_the_queue_waits_from_the_first_pass_after_a_restart(
    db_session, printer_factory, scheduler, farm, raw_gcode_source
):
    """Focus 5: a fresh scheduler (a restart) holds the printer before its first dispatch."""
    await _setting(db_session, "queue_drying_block", "true")
    printer, item = await _printer_with_work(db_session, printer_factory, raw_gcode_source)
    await _running_run(db_session, printer)

    start = AsyncMock()
    with patch.object(PrintScheduler, "_start_print", start):
        await scheduler.check_queue()

    assert start.await_count == 0
    await db_session.refresh(item)
    assert item.status == "pending"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_queue_keeps_waiting_through_the_cooling_tail(
    db_session, printer_factory, scheduler, farm, raw_gcode_source
):
    """Focus 1: dry_time is 0 while the unit cools; the hold is the schedule's to drop."""
    await _setting(db_session, "queue_drying_block", "true")
    printer, _item = await _printer_with_work(db_session, printer_factory, raw_gcode_source)
    await _running_run(db_session, printer)
    farm.state.raw_data = {"ams": [_unit(dry_time=0, dry_status=3)]}

    start = AsyncMock()
    with patch.object(PrintScheduler, "_start_print", start):
        await scheduler.check_queue()
        await scheduler.check_queue()

    assert start.await_count == 0


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("model", "firmware", "dry_through"),
    [("X1C", "01.11.00.00", False), ("H2D", "01.03.00.00", True)],
)
async def test_with_the_block_off_the_print_takes_the_printer_on_every_model(
    db_session, printer_factory, scheduler, farm, raw_gcode_source, model, firmware, dry_through
):
    """Owner decision 6: even a model that dries through a print stops a scheduled cycle."""
    if dry_through:
        await _setting(db_session, "print_drying_enabled", "true")
    farm.get_model.return_value = model
    farm.state.firmware_version = firmware
    printer, _item = await _printer_with_work(db_session, printer_factory, raw_gcode_source, model=model)
    run = await _running_run(db_session, printer)

    start = AsyncMock()
    with patch.object(PrintScheduler, "_start_print", start):
        await scheduler.check_queue()

    assert start.await_count == 1
    farm.send_drying_command.assert_any_call(printer.id, 0, 0, 0, mode=0)
    await db_session.refresh(run)
    assert run.status == "pending"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_failing_tick_does_not_stop_the_queue(db_session, printer_factory, scheduler, farm, raw_gcode_source):
    """Focus 4."""
    await _printer_with_work(db_session, printer_factory, raw_gcode_source)

    start = AsyncMock()
    with (
        patch.object(PrintScheduler, "_start_print", start),
        patch.object(sd, "tick", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        await scheduler.check_queue()

    assert start.await_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_failing_preemption_does_not_stop_the_print(
    db_session, printer_factory, scheduler, farm, raw_gcode_source
):
    printer, _item = await _printer_with_work(db_session, printer_factory, raw_gcode_source)
    await _running_run(db_session, printer)

    start = AsyncMock()
    with (
        patch.object(PrintScheduler, "_start_print", start),
        patch.object(sd, "preempt_for_print", AsyncMock(side_effect=RuntimeError("database is locked"))),
    ):
        await scheduler.check_queue()

    assert start.await_count == 1
