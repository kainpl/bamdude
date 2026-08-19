"""Auto-drying must not re-arm into a threshold it can never reach.

Ported from upstream #2770. An H2D armed five 12-hour cycles inside four hours,
one of them six seconds after the previous ended, and none ran more than a
couple of hours.

Two things combine. The firmware ends a cycle when it decides the filament is
dry rather than when the clock runs out, and says nothing is wrong doing it.
⚠️ And **an AMS reports higher relative humidity while it is warm than once it
has cooled** — the same unit read 10-13% cold and 15-20% through every cycle.
With the threshold at 14%, the reading at the moment a cycle ended was always
still above it, so the next pass armed another twelve hours. Nothing counted,
nothing waited.

⚠️ The cooldown is stored separately from the judgement, and dropping the
judgement must not drop the clock. Upstream shipped the two together, so a unit
a point or two above the threshold — whose reading dips below it as the AMS
cools and comes back once warm — wiped its own history and re-armed
immediately, which is the oscillation the cooldown exists to ride out. We start
from the repaired behaviour.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.print_scheduler import (
    _AUTO_DRY_MAX_UNPRODUCTIVE_CYCLES,
    _AUTO_DRY_REARM_COOLDOWN_SECONDS,
    PrintScheduler,
)


def _ams(humidity: int, *, dry_time: int = 0) -> dict:
    return {
        "id": 0,
        "module_type": "n3f",
        "dry_time": dry_time,
        "humidity_raw": str(humidity),
        "dry_sf_reason": [],
        "tray": [{"tray_type": "PLA"}],
    }


def _state(humidity: int, *, dry_time: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        raw_data={"ams": [_ams(humidity, dry_time=dry_time)]},
        firmware_version="01.03.00.00",
        state="IDLE",
    )


def _db(**overrides) -> AsyncMock:
    values = {
        "queue_drying_enabled": "true",
        "ambient_drying_enabled": "true",
        "print_drying_enabled": "false",
        "ams_humidity_fair": "14",
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


async def _tick(scheduler, pm, humidity: int, *, dry_time: int = 0) -> None:
    pm.get_status.return_value = _state(humidity, dry_time=dry_time)
    await scheduler._check_auto_drying(_db(), [], set())


class TestTheCooldown:
    @pytest.mark.asyncio
    async def test_a_cycle_is_not_re_armed_the_moment_it_ends(self, scheduler, pm):
        """The reported six-second re-arm."""
        await _tick(scheduler, pm, 20)  # arms
        await _tick(scheduler, pm, 18, dry_time=700)  # running
        pm.send_drying_command.reset_mock()

        await _tick(scheduler, pm, 18)  # ended, still above the threshold

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_re_arms_once_the_cooldown_has_passed(self, scheduler, pm):
        await _tick(scheduler, pm, 20)
        await _tick(scheduler, pm, 18, dry_time=700)
        await _tick(scheduler, pm, 18)
        pm.send_drying_command.reset_mock()

        scheduler._dry_unit_history[(1, 0)]["ended_at"] = time.monotonic() - _AUTO_DRY_REARM_COOLDOWN_SECONDS - 1

        await _tick(scheduler, pm, 18)

        pm.send_drying_command.assert_called_once()


class TestTheUnproductiveCap:
    @staticmethod
    async def _unproductive_round(scheduler, pm, humidity: int) -> None:
        """One armed cycle that ends with the reading no lower.

        The cooldown is stepped over deliberately — it is tested above, and
        leaving it in would make every round here wait half an hour.
        """
        scheduler._dry_unit_history.setdefault((1, 0), {})["ended_at"] = 0.0
        await _tick(scheduler, pm, humidity)
        await _tick(scheduler, pm, humidity, dry_time=700)
        await _tick(scheduler, pm, humidity)

    @pytest.mark.asyncio
    async def test_it_gives_up_after_two_cycles_that_change_nothing(self, scheduler, pm):
        for _ in range(_AUTO_DRY_MAX_UNPRODUCTIVE_CYCLES):
            await self._unproductive_round(scheduler, pm, 15)
        pm.send_drying_command.reset_mock()
        scheduler._dry_unit_history[(1, 0)]["ended_at"] = 0.0

        await _tick(scheduler, pm, 15)

        assert scheduler._dry_unit_history[(1, 0)]["suspended"] is True
        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reading_that_keeps_falling_keeps_drying(self, scheduler, pm):
        """⚠️ 40 → 37 → 35 is a genuinely wet spool in a humid room. It must be
        allowed to keep going however far the threshold still is — progress is
        measured against the best reading so far, not against the threshold."""
        for humidity in (37, 35, 33, 31):
            await self._unproductive_round(scheduler, pm, humidity)

        assert not scheduler._dry_unit_history[(1, 0)].get("suspended")

    @pytest.mark.asyncio
    async def test_a_sensor_wobbling_by_one_point_is_not_progress(self, scheduler, pm):
        """⚠️ Why progress is measured against the RUNNING MINIMUM and not
        against the previous cycle.

        The sequence matters: one genuine drop (20 → 15), then a sensor hovering
        between 15 and 16. Judged against the previous end, every second cycle
        reads as progress — 15 beats 16 — the counter resets, and the loop runs
        for ever. Judged against the best so far, nothing after the 15 is
        progress and the unit is given up on.

        ⚠️ A shorter wobble would not prove this: 15, 16, 15 suspends under both
        rules, so a test built on it passes with the wrong comparison in place —
        measured."""
        for humidity in (20, 15, 16, 15, 16, 15):
            await self._unproductive_round(scheduler, pm, humidity)

        assert scheduler._dry_unit_history[(1, 0)]["suspended"] is True


class TestLiftingTheSuspension:
    @pytest.mark.asyncio
    async def test_a_reading_below_the_threshold_clears_the_judgement(self, scheduler, pm):
        scheduler._dry_unit_history[(1, 0)] = {
            "suspended": True,
            "unproductive": 3,
            "best_end_humidity": 15,
            "ended_at": 0.0,
        }

        await _tick(scheduler, pm, 10)

        state = scheduler._dry_unit_history[(1, 0)]
        assert "suspended" not in state
        assert "unproductive" not in state
        assert "best_end_humidity" not in state

    @pytest.mark.asyncio
    async def test_but_keeps_the_clock(self, scheduler, pm):
        """⚠️ The fault upstream shipped and repaired only later. An AMS reads
        higher warm than cool, so a unit a point above the threshold dips below
        it as it cools — and dropping the whole entry there wiped the cooldown
        with it, letting the unit re-arm the moment it warmed back up."""
        ended_at = time.monotonic()
        scheduler._dry_unit_history[(1, 0)] = {"suspended": True, "unproductive": 3, "ended_at": ended_at}

        await _tick(scheduler, pm, 10)  # cooled, below the threshold
        pm.send_drying_command.reset_mock()
        await _tick(scheduler, pm, 16)  # warm again, back above it

        assert scheduler._dry_unit_history[(1, 0)]["ended_at"] == ended_at
        pm.send_drying_command.assert_not_called()


class TestAStopWeCausedIsNotJudged:
    @pytest.mark.asyncio
    async def test_forgetting_a_cycle_does_not_count_it(self, scheduler, pm):
        """⚠️ An install that dries between queue jobs would otherwise suspend
        its own auto-drying after two prints interrupted a dry — exactly the
        install queue-drying exists for."""
        await _tick(scheduler, pm, 20)
        await _tick(scheduler, pm, 18, dry_time=700)

        scheduler.forget_auto_dry_cycle(1, 0)
        await _tick(scheduler, pm, 18)

        assert scheduler._dry_unit_history[(1, 0)].get("unproductive", 0) == 0

    @pytest.mark.asyncio
    async def test_but_the_cooldown_still_applies(self, scheduler, pm):
        await _tick(scheduler, pm, 20)
        await _tick(scheduler, pm, 18, dry_time=700)
        scheduler.forget_auto_dry_cycle(1, 0)
        pm.send_drying_command.reset_mock()

        await _tick(scheduler, pm, 18)

        pm.send_drying_command.assert_not_called()


class TestNeitherGuardTouchesARunningCycle:
    @pytest.mark.asyncio
    async def test_a_suspended_unit_that_is_drying_is_left_alone(self, scheduler, pm):
        scheduler._dry_unit_history[(1, 0)] = {"suspended": True, "unproductive": 3, "ended_at": 0.0}

        await _tick(scheduler, pm, 20, dry_time=700)

        assert not any(call.args[2:] == (0, 0) for call in pm.send_drying_command.call_args_list)
