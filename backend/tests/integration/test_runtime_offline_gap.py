"""A printer's time offline is not print time (upstream 175758d25).

The runtime counter adds the time since its last tick while a printer is
RUNNING. A printer that dropped off mid-print and came back hours later with a
print running was credited with the whole gap on its first tick, and
hours-based maintenance came due early. The stored timestamp is now cleared
when a printer this process saw connected goes offline (or has no state), so
only the stretch between two consecutive RUNNING ticks is ever added. A print
that ran on through a restart of BamDude is still counted from the stored
timestamp: those printers have not been seen connected yet, for a grace period.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app import main as main_module
from backend.app.models.printer import Printer
from backend.app.services import printer_deletion

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

RUNNING = SimpleNamespace(connected=True, state="RUNNING")
OFFLINE = SimpleNamespace(connected=False, state="RUNNING")  # the cache keeps the last state it heard
IDLE = SimpleNamespace(connected=True, state="IDLE")
PAUSED = SimpleNamespace(connected=True, state="PAUSE")


@pytest.fixture(autouse=True)
def _forget_seen():
    main_module._runtime_seen_connected.clear()
    yield
    main_module._runtime_seen_connected.clear()


async def _ticks(test_engine, steps: list) -> None:
    """Run the tracker for one tick per step, then stop it.

    A step is the state the printer shows at that tick, or a callable run before the tick that returns it.
    """
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    feed = iter(steps)
    current = {"state": None}
    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > len(steps):
            raise asyncio.CancelledError
        step = next(feed)
        if callable(step):
            step = await step()
        current["state"] = step

    with (
        patch.object(main_module, "async_session", maker),
        patch.object(main_module.asyncio, "sleep", fake_sleep),
        patch.object(main_module.printer_manager, "get_status", lambda _pid: current["state"]),
    ):
        try:
            await main_module.track_printer_runtime()
        except asyncio.CancelledError:
            pass


def _ago(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


async def _printer(db_session, printer_factory, last_update) -> Printer:
    printer = await printer_factory()
    printer.runtime_seconds = 1000
    printer.last_runtime_update = last_update
    await db_session.commit()
    return printer


def _backdate(test_engine, printer_id: int, **delta):
    """A step: the hours pass while the printer is away (the stored timestamp, if any, grows old)."""

    def step(state):
        async def run():
            maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
            async with maker() as db:
                printer = await db.get(Printer, printer_id)
                if printer.last_runtime_update is not None:
                    printer.last_runtime_update = _ago(**delta)
                    await db.commit()
            return state

        return run

    return step


async def test_a_disconnect_clears_the_timestamp(test_engine, db_session, printer_factory):
    printer = await _printer(db_session, printer_factory, _ago(seconds=60))

    await _ticks(test_engine, [RUNNING, OFFLINE])

    await db_session.refresh(printer)
    assert printer.last_runtime_update is None


async def test_the_hours_offline_are_not_added_when_it_comes_back_printing(test_engine, db_session, printer_factory):
    printer = await _printer(db_session, printer_factory, None)
    hours_pass = _backdate(test_engine, printer.id, hours=8)

    # Printing, drops off, eight hours later it is back with a print running.
    await _ticks(test_engine, [RUNNING, hours_pass(OFFLINE), hours_pass(RUNNING)])

    await db_session.refresh(printer)
    assert printer.runtime_seconds < 1000 + 60
    assert printer.last_runtime_update is not None  # a fresh interval started at the return


@pytest.mark.parametrize("parked", [{"is_active": False}, {"archived": True}])
async def test_a_parked_printer_does_not_keep_its_timestamp(test_engine, db_session, printer_factory, parked):
    """Maintenance mode and retirement take a printer out of the tracker's loop; its stamp must not wait there."""
    printer = await _printer(db_session, printer_factory, _ago(hours=8))
    for name, value in parked.items():
        setattr(printer, name, value)
    await db_session.commit()

    await _ticks(test_engine, [RUNNING])

    await db_session.refresh(printer)
    assert printer.last_runtime_update is None
    assert printer.runtime_seconds == 1000


async def test_a_printer_without_a_state_is_offline_too(test_engine, db_session, printer_factory):
    printer = await _printer(db_session, printer_factory, _ago(seconds=60))
    hours_pass = _backdate(test_engine, printer.id, hours=8)

    await _ticks(test_engine, [RUNNING, hours_pass(None), hours_pass(RUNNING)])

    await db_session.refresh(printer)
    assert printer.runtime_seconds < 1000 + 120


@pytest.mark.parametrize("quiet", [IDLE, PAUSED], ids=["idle", "paused"])
async def test_an_idle_or_paused_stretch_is_not_added(test_engine, db_session, printer_factory, quiet):
    printer = await _printer(db_session, printer_factory, None)
    hours_pass = _backdate(test_engine, printer.id, hours=5)

    await _ticks(test_engine, [RUNNING, hours_pass(quiet), hours_pass(RUNNING)])

    await db_session.refresh(printer)
    assert printer.runtime_seconds < 1000 + 60


async def test_two_running_ticks_add_the_time_between_them(test_engine, db_session, printer_factory):
    printer = await _printer(db_session, printer_factory, None)
    half_a_minute = _backdate(test_engine, printer.id, seconds=30)

    await _ticks(test_engine, [RUNNING, half_a_minute(RUNNING)])

    await db_session.refresh(printer)
    assert 1000 + 30 <= printer.runtime_seconds <= 1000 + 35


async def test_a_restart_still_counts_the_stored_timestamp(test_engine, db_session, printer_factory):
    printer = await _printer(db_session, printer_factory, _ago(minutes=30))
    connecting = SimpleNamespace(connected=False, state="unknown")

    # Still connecting after the restart, then printing: the print ran on while BamDude was down.
    await _ticks(test_engine, [connecting, None, RUNNING])

    await db_session.refresh(printer)
    assert printer.runtime_seconds >= 1000 + 29 * 60


async def test_a_printer_still_offline_after_the_startup_grace_is_not_credited(
    test_engine, db_session, printer_factory, monkeypatch
):
    """It never came back during the grace: the stored timestamp is from before a gap, not a print that ran on."""
    monkeypatch.setattr(main_module, "RUNTIME_STARTUP_GRACE_SECONDS", -1)
    printer = await _printer(db_session, printer_factory, _ago(hours=10))

    await _ticks(test_engine, [SimpleNamespace(connected=False, state="unknown"), RUNNING])

    await db_session.refresh(printer)
    assert printer.runtime_seconds < 1000 + 60


async def test_a_clear_that_failed_is_tried_again(test_engine, db_session, printer_factory):
    printer = await _printer(db_session, printer_factory, _ago(seconds=60))
    real_commit = AsyncSession.commit
    calls = {"n": 0}

    async def flaky_commit(self):
        calls["n"] += 1
        if calls["n"] == 2:  # the commit of the first offline tick
            raise RuntimeError("database is locked")
        return await real_commit(self)

    with patch.object(AsyncSession, "commit", flaky_commit):
        await _ticks(test_engine, [RUNNING, OFFLINE, OFFLINE])

    await db_session.refresh(printer)
    assert calls["n"] >= 3
    assert printer.last_runtime_update is None
    assert printer.id not in main_module._runtime_seen_connected


async def test_a_deleted_printer_is_forgotten_by_the_tracker():
    assert ("backend.app.main", "_runtime_seen_connected") in printer_deletion._ID_KEYED
