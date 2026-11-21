"""Auto-drying waits for humidity that STAYS above the threshold.

Adapted from upstream #2518. Opening an AMS lid to swap a spool puts room air
on the sensor: one reading above the threshold, back to normal a few minutes
later. Auto-drying armed a full preset cycle (up to twelve hours) on that one
reading, and nothing cancelled it when the reading fell back.

The wait is a property of auto-drying, not of ambient mode: every
humidity-triggered start with no scheduled job behind it waits, a mid-print one
included. A printer with a scheduled job (queue drying) starts at once — that
drying has a deadline.

⚠️ The streak is a third memory beside the claim (``_armed_dry_cycles``) and
the judgement (``_dry_unit_history``), and like them it answers one question
only — "how long has this unit read wet". It is never a reason to stop a cycle.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.print_scheduler import (
    _AUTO_DRY_REARM_COOLDOWN_SECONDS,
    PrintScheduler,
)

UNIT = (1, 0)
WAIT = 15 * 60


def _ams(humidity: int | None, *, dry_time: int = 0) -> dict:
    unit = {
        "id": 0,
        "module_type": "n3f",
        "dry_time": dry_time,
        "dry_sf_reason": [],
        "tray": [{"tray_type": "PLA"}],
    }
    if humidity is not None:
        unit["humidity_raw"] = str(humidity)
    return unit


def _state(humidity: int | None, *, dry_time: int = 0, state: str = "IDLE") -> SimpleNamespace:
    return SimpleNamespace(
        raw_data={"ams": [_ams(humidity, dry_time=dry_time)]},
        firmware_version="01.03.00.00",
        state=state,
    )


def _db(**overrides) -> AsyncMock:
    values = {
        "queue_drying_enabled": "false",
        "ambient_drying_enabled": "true",
        "print_drying_enabled": "false",
        "ams_humidity_fair": "60",
        "queue_drying_block": "false",
    }
    values.update(overrides)
    db = AsyncMock()

    async def execute(statement, *args, **kwargs):
        result = MagicMock()
        try:
            params = list(statement.compile(compile_kwargs={"literal_binds": False}).params.values())
        except Exception:
            params = []
        for key, value in values.items():
            if key in params:
                result.scalar_one_or_none.return_value = SimpleNamespace(value=value)
                return result
        if "printer" in str(statement).lower():
            printer = MagicMock()
            printer.id = 1
            printer.is_active = True
            scalars = MagicMock()
            scalars.__iter__ = MagicMock(return_value=iter([printer]))
            result.scalars.return_value = scalars
            result.scalar_one_or_none.return_value = "Printer One"
            return result
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.fixture
def scheduler():
    instance = PrintScheduler()
    instance._is_printer_idle = MagicMock(return_value=True)
    return instance


@pytest.fixture
def pm():
    with patch("backend.app.services.print_scheduler.printer_manager") as mock:
        mock.is_connected.return_value = True
        mock.get_model.return_value = "H2D"
        mock.send_drying_command.return_value = True
        yield mock


async def _tick(scheduler, pm, humidity, *, dry_time: int = 0, items=(), state: str = "IDLE", **settings) -> None:
    pm.get_status.return_value = _state(humidity, dry_time=dry_time, state=state)
    busy = {1} if state != "IDLE" else set()
    await scheduler._check_auto_drying(_db(**settings), list(items), busy)


def _age(scheduler, seconds: float, *, unseen_for: float = 0.0) -> None:
    """Move the unit's streak back in time: it began ``seconds`` ago and was
    last observed ``unseen_for`` seconds ago."""
    streak = scheduler._auto_dry_above[UNIT]
    streak["since"] -= seconds
    streak["last"] -= unseen_for


def _scheduled_item(*, hours_ahead: float = 2) -> SimpleNamespace:
    when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours_ahead)
    return SimpleNamespace(queue_id=1, status="pending", scheduled_time=when, manual_start=False)


class TestTheWait:
    @pytest.mark.asyncio
    async def test_one_reading_above_the_threshold_arms_nothing(self, scheduler, pm):
        """The lid spike. The setting row is absent here: the wait is on by default."""
        await _tick(scheduler, pm, 75)

        pm.send_drying_command.assert_not_called()
        assert UNIT in scheduler._auto_dry_above
        assert 1 not in scheduler._drying_in_progress

    @pytest.mark.asyncio
    async def test_a_reading_held_for_the_whole_wait_arms_once(self, scheduler, pm):
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT - 60)
        await _tick(scheduler, pm, 75)
        pm.send_drying_command.assert_not_called()

        _age(scheduler, 120)
        await _tick(scheduler, pm, 75)

        pm.send_drying_command.assert_called_once_with(1, 0, 45, 12, mode=1, filament="PLA")
        assert 1 in scheduler._drying_in_progress

    @pytest.mark.asyncio
    async def test_the_configured_minutes_are_the_wait(self, scheduler, pm):
        await _tick(scheduler, pm, 75, auto_drying_sustained_minutes="30")
        _age(scheduler, WAIT + 60)
        await _tick(scheduler, pm, 75, auto_drying_sustained_minutes="30")
        pm.send_drying_command.assert_not_called()

        _age(scheduler, WAIT)
        await _tick(scheduler, pm, 75, auto_drying_sustained_minutes="30")

        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_minutes_is_the_old_behaviour(self, scheduler, pm):
        await _tick(scheduler, pm, 75, auto_drying_sustained_minutes="0")

        pm.send_drying_command.assert_called_once_with(1, 0, 45, 12, mode=1, filament="PLA")
        assert not scheduler._auto_dry_above

    @pytest.mark.asyncio
    async def test_switching_the_wait_off_drops_every_streak(self, scheduler, pm):
        """No stale streak is waiting when the wait is switched back on."""
        await _tick(scheduler, pm, 75)
        assert scheduler._auto_dry_above

        # Humidity is fine on this pass, so nothing would touch the unit's entry.
        await _tick(scheduler, pm, None, auto_drying_sustained_minutes="0")

        assert not scheduler._auto_dry_above


class TestWhatResetsTheStreak:
    @pytest.mark.asyncio
    async def test_one_reading_at_the_threshold_resets_it(self, scheduler, pm):
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT - 60)

        await _tick(scheduler, pm, 60)  # at the threshold: not above it
        assert UNIT not in scheduler._auto_dry_above

        await _tick(scheduler, pm, 75)
        _age(scheduler, 120)
        await _tick(scheduler, pm, 75)

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_reading_keeps_it(self, scheduler, pm):
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT - 60)
        since = scheduler._auto_dry_above[UNIT]["since"]

        await _tick(scheduler, pm, None)

        assert scheduler._auto_dry_above[UNIT]["since"] == since
        pm.send_drying_command.assert_not_called()

        _age(scheduler, 120)
        await _tick(scheduler, pm, 75)
        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_unit_nobody_watched_for_too_long_starts_over(self, scheduler, pm):
        """A printer that was printing or offline says nothing about the minutes in between."""
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT + 600, unseen_for=4 * scheduler._check_interval + 5)

        await _tick(scheduler, pm, 75)

        pm.send_drying_command.assert_not_called()
        assert time.monotonic() - scheduler._auto_dry_above[UNIT]["since"] < 5

    @pytest.mark.asyncio
    async def test_a_short_gap_does_not_start_it_over(self, scheduler, pm):
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT + 600, unseen_for=scheduler._check_interval + 5)

        await _tick(scheduler, pm, 75)

        pm.send_drying_command.assert_called_once()


class TestWhoDoesNotWait:
    @pytest.mark.asyncio
    async def test_a_printer_with_a_scheduled_job_starts_at_once(self, scheduler, pm):
        await _tick(scheduler, pm, 75, items=[_scheduled_item()], queue_drying_enabled="true")

        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_scheduled_job_is_no_deadline_while_queue_drying_is_off(self, scheduler, pm):
        """With queue drying off nothing dries FOR the job: the start is an
        ambient one, and the lid spike is the same."""
        await _tick(scheduler, pm, 75, items=[_scheduled_item()])

        pm.send_drying_command.assert_not_called()


class TestMidPrint:
    @pytest.mark.asyncio
    async def test_a_mid_print_start_waits_with_ambient_off(self, scheduler, pm):
        settings = {
            "queue_drying_enabled": "true",
            "ambient_drying_enabled": "false",
            "print_drying_enabled": "true",
        }
        scheduler._is_printer_idle = MagicMock(return_value=False)

        await _tick(scheduler, pm, 75, state="RUNNING", **settings)
        pm.send_drying_command.assert_not_called()

        _age(scheduler, WAIT + 60)
        await _tick(scheduler, pm, 75, state="RUNNING", **settings)

        pm.send_drying_command.assert_called_once_with(1, 0, 40, 12, mode=1, filament="PLA")


class TestTheCooldownAndTheWaitOverlap:
    @pytest.mark.asyncio
    async def test_a_cycle_spends_the_streak_that_armed_it(self, scheduler, pm):
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT + 60)
        await _tick(scheduler, pm, 75)  # arms

        assert UNIT not in scheduler._auto_dry_above

        await _tick(scheduler, pm, 70, dry_time=700)  # running
        assert UNIT not in scheduler._auto_dry_above

    @pytest.mark.asyncio
    async def test_the_wait_runs_through_the_cooldown(self, scheduler, pm):
        """A cycle that ends wet re-arms at max(cooldown, wait), never cooldown + wait."""
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT + 60)
        await _tick(scheduler, pm, 75)
        await _tick(scheduler, pm, 70, dry_time=700)
        pm.send_drying_command.reset_mock()

        await _tick(scheduler, pm, 70)  # ended, still wet: the new streak starts here
        assert UNIT in scheduler._auto_dry_above
        pm.send_drying_command.assert_not_called()

        # The whole cooldown passes with the unit wet and watched.
        _age(scheduler, _AUTO_DRY_REARM_COOLDOWN_SECONDS + 1)
        scheduler._dry_unit_history[UNIT]["ended_at"] -= _AUTO_DRY_REARM_COOLDOWN_SECONDS + 1
        await _tick(scheduler, pm, 70)

        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_cooldown_alone_is_not_enough(self, scheduler, pm):
        """The cooldown is over but the unit has only just read wet again."""
        scheduler._dry_unit_history[UNIT] = {"ended_at": time.monotonic() - _AUTO_DRY_REARM_COOLDOWN_SECONDS - 1}

        await _tick(scheduler, pm, 70)

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cycle_we_stopped_spends_the_streak(self, scheduler, pm):
        scheduler._auto_dry_above[UNIT] = {"since": time.monotonic() - 3600, "last": time.monotonic()}

        scheduler.forget_auto_dry_cycle(1, 0)

        assert UNIT not in scheduler._auto_dry_above

    @pytest.mark.asyncio
    async def test_a_printer_that_went_away_takes_its_streak(self, scheduler, pm):
        scheduler._auto_dry_above[UNIT] = {"since": time.monotonic() - 3600, "last": time.monotonic()}
        pm.get_status.return_value = None

        scheduler._sync_drying_state()

        assert not scheduler._auto_dry_above


class TestTheSetting:
    def test_the_two_defaults_agree(self):
        """A missing row must mean the same minutes to the Settings page and to the scheduler."""
        from backend.app.schemas.settings import AppSettings
        from backend.app.services.print_scheduler import AUTO_DRY_SUSTAINED_DEFAULT_MINUTES

        assert AppSettings().auto_drying_sustained_minutes == AUTO_DRY_SUSTAINED_DEFAULT_MINUTES == 15

    @pytest.mark.parametrize("minutes", [-1, 241])
    def test_minutes_outside_the_range_are_refused(self, minutes):
        from pydantic import ValidationError

        from backend.app.schemas.settings import AppSettingsUpdate

        with pytest.raises(ValidationError):
            AppSettingsUpdate(auto_drying_sustained_minutes=minutes)

    @pytest.mark.asyncio
    async def test_an_unreadable_stored_value_is_the_default(self, scheduler, pm):
        await _tick(scheduler, pm, 75, auto_drying_sustained_minutes="None")

        pm.send_drying_command.assert_not_called()
        assert UNIT in scheduler._auto_dry_above


class TestTheStreakNeverStopsAnything:
    @pytest.mark.asyncio
    async def test_a_manual_cycle_on_a_wet_unit_is_left_alone(self, scheduler, pm):
        await _tick(scheduler, pm, 75)
        _age(scheduler, WAIT + 60)

        # Somebody starts a cycle by hand before ours would have.
        await _tick(scheduler, pm, 75, dry_time=700)
        pm.get_status.return_value = _state(75, dry_time=700)
        await scheduler._stop_drying(1)

        pm.send_drying_command.assert_not_called()
        assert UNIT not in scheduler._armed_dry_cycles

    @pytest.mark.asyncio
    async def test_a_unit_a_schedule_reserved_gets_no_streak(self, scheduler, pm):
        scheduler._scheduled_dry_units = {UNIT}

        await _tick(scheduler, pm, 75)

        assert not scheduler._auto_dry_above
        pm.send_drying_command.assert_not_called()
