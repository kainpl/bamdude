"""Block mode and queue pre-drying.

Adapted from upstream e05a91744 (leg 4) and 1ffcfa260 (leg 3).

«Wait for drying to complete» (``queue_drying_block``) refused a NEW cycle on
any printer with a pending row. Queue drying dries only printers with a
scheduled row — every one of which has a pending row — so the two settings
together never started a cycle at all, and in ambient mode a job scheduled for
next week kept its printer undried for the week.

Now only a row that could start NOW refuses a new cycle: due, not
``manual_start``, its queue not paused or in error. Anything else cannot start
without time passing or a person acting, so the printer is free to dry ahead of
it; the hold at the job's own time is unchanged.

Queue pre-drying also looks at most twelve hours ahead (the longest preset), on
both tiers: a per-printer scheduled row, and a scheduled «any printer of model
X» AutoQueue row through its candidate printers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.print_scheduler import PrintScheduler


def _state(humidity: int = 75, *, dry_time: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "module_type": "n3f",
                    "dry_time": dry_time,
                    "humidity_raw": str(humidity),
                    "dry_sf_reason": [],
                    "tray": [{"tray_type": "PLA"}],
                }
            ]
        },
        firmware_version="01.03.00.00",
        state="IDLE",
    )


def _db(**overrides) -> AsyncMock:
    values = {
        "queue_drying_enabled": "true",
        "ambient_drying_enabled": "false",
        "print_drying_enabled": "false",
        "ams_humidity_fair": "60",
        "queue_drying_block": "true",
        # The sustained-humidity wait has its own file; off so a start is one tick.
        "auto_drying_sustained_minutes": "0",
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
            return result
        result.scalar_one_or_none.return_value = None
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


def _row(
    *,
    hours: float | None = None,
    manual_start: bool = False,
    is_paused: bool = False,
    status: str = "idle",
) -> SimpleNamespace:
    """A pending per-printer row; ``hours`` is its scheduled time from now (None = unscheduled)."""
    when = None
    if hours is not None:
        when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)
    return SimpleNamespace(
        queue_id=1,
        status="pending",
        scheduled_time=when,
        manual_start=manual_start,
        queue=SimpleNamespace(is_paused=is_paused, status=status),
    )


@pytest.fixture
def scheduler():
    instance = PrintScheduler()
    instance._is_printer_idle = MagicMock(return_value=True)
    instance._scheduled_auto_queue_printers = AsyncMock(return_value=set())
    return instance


@pytest.fixture
def pm():
    with patch("backend.app.services.print_scheduler.printer_manager") as mock:
        mock.is_connected.return_value = True
        mock.get_model.return_value = "H2D"
        mock.send_drying_command.return_value = True
        mock.get_status.return_value = _state()
        yield mock


class TestBlockModeRefusesOnlyForARowThatCouldStartNow:
    @pytest.mark.asyncio
    async def test_a_row_scheduled_for_later_is_dried_ahead_of(self, scheduler, pm):
        """Queue drying + block mode: the combination that never started a cycle."""
        await scheduler._check_auto_drying(_db(), [_row(hours=3)], set())

        pm.send_drying_command.assert_called_once_with(1, 0, 45, 12, mode=1, filament="PLA")

    @pytest.mark.asyncio
    async def test_a_row_whose_time_has_come_refuses_a_new_cycle(self, scheduler, pm):
        await scheduler._check_auto_drying(_db(), [_row(hours=-0.1)], set())

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_is_block_mode_that_refuses(self, scheduler, pm):
        """The same due row with block mode off: the cycle starts."""
        await scheduler._check_auto_drying(_db(queue_drying_block="false"), [_row(hours=-0.1)], set())

        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_unscheduled_row_refuses_a_new_cycle(self, scheduler, pm):
        await scheduler._check_auto_drying(_db(ambient_drying_enabled="true"), [_row()], set())

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            _row(manual_start=True),
            _row(is_paused=True),
            _row(status="paused"),
            _row(status="error"),
            _row(hours=30),
        ],
        ids=["manual-start", "queue-paused-by-operator", "queue-paused", "queue-in-error", "next-week"],
    )
    async def test_a_row_that_cannot_start_now_leaves_the_printer_free_to_dry(self, scheduler, pm, row):
        await scheduler._check_auto_drying(_db(ambient_drying_enabled="true"), [row], set())

        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_running_cycle_is_still_let_through(self, scheduler, pm):
        """Block mode never stops what is already drying, due row or not."""
        pm.get_status.return_value = _state(dry_time=600)
        scheduler._drying_in_progress[1] = 1.0
        scheduler._armed_dry_cycles[(1, 0)] = 1.0

        await scheduler._check_auto_drying(_db(), [_row(hours=-0.1)], set())

        pm.send_drying_command.assert_not_called()
        assert 1 in scheduler._drying_in_progress


class TestTheTwelveHourHorizon:
    @pytest.mark.asyncio
    async def test_a_row_inside_the_horizon_is_a_reason_to_dry(self, scheduler, pm):
        await scheduler._check_auto_drying(_db(queue_drying_block="false"), [_row(hours=11)], set())

        pm.send_drying_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_row_beyond_the_horizon_is_not(self, scheduler, pm):
        await scheduler._check_auto_drying(_db(queue_drying_block="false"), [_row(hours=13)], set())

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cycle_dried_for_a_row_beyond_the_horizon_is_not_kept(self, scheduler, pm):
        """Queue-only mode keeps its own rule: no scheduled print in reach, no drying of ours."""
        pm.get_status.return_value = _state(dry_time=600)
        scheduler._drying_in_progress[1] = 1.0
        scheduler._armed_dry_cycles[(1, 0)] = 1.0

        await scheduler._check_auto_drying(_db(queue_drying_block="false"), [_row(hours=13)], set())

        pm.send_drying_command.assert_called_once_with(1, 0, 0, 0, mode=0)


class TestAScheduledAnyPrinterJob:
    @pytest.mark.asyncio
    async def test_its_candidate_printer_is_dried_ahead_of_it(self, scheduler, pm):
        scheduler._scheduled_auto_queue_printers = AsyncMock(return_value={1})

        await scheduler._check_auto_drying(_db(), [], set())

        pm.send_drying_command.assert_called_once_with(1, 0, 45, 12, mode=1, filament="PLA")

    @pytest.mark.asyncio
    async def test_a_printer_it_cannot_run_on_is_not(self, scheduler, pm):
        scheduler._scheduled_auto_queue_printers = AsyncMock(return_value={2})

        await scheduler._check_auto_drying(_db(), [], set())

        pm.send_drying_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_with_none_scheduled_queue_drying_still_stops_and_returns(self, scheduler, pm):
        pm.get_status.return_value = _state(dry_time=600)
        scheduler._drying_in_progress[1] = 1.0
        scheduler._armed_dry_cycles[(1, 0)] = 1.0

        await scheduler._check_auto_drying(_db(), [], set())

        pm.send_drying_command.assert_called_once_with(1, 0, 0, 0, mode=0)

    @pytest.mark.asyncio
    async def test_the_auto_queue_is_not_asked_while_queue_drying_is_off(self, scheduler, pm):
        await scheduler._check_auto_drying(
            _db(queue_drying_enabled="false", ambient_drying_enabled="true", queue_drying_block="false"), [], set()
        )

        scheduler._scheduled_auto_queue_printers.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_lookup_never_stops_the_pass(self, scheduler, pm):
        scheduler._scheduled_auto_queue_printers = AsyncMock(side_effect=RuntimeError("boom"))

        await scheduler._check_auto_drying(_db(), [_row(hours=3)], set())

        pm.send_drying_command.assert_called_once()
