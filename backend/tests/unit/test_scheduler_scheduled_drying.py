"""The scheduler's side: reservations, holds, preemption, a failing tick (Review Focus 1, 4, 5)."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler


def test_auto_drying_keeps_the_hold_of_a_scheduled_run():
    scheduler = PrintScheduler()
    scheduler._drying_in_progress = {7: 1.0}
    scheduler._scheduled_drying_printers = {7}
    scheduler._release_drying_hold(7)
    assert 7 in scheduler._drying_in_progress


@pytest.mark.asyncio
async def test_a_failing_tick_never_reaches_the_queue():
    scheduler = PrintScheduler()
    with patch(
        "backend.app.services.print_scheduler.scheduled_drying.tick", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        await scheduler._tick_scheduled_drying(set())  # must not raise
    assert scheduler._scheduled_dry_units == set()


@pytest.mark.asyncio
async def test_a_restart_restores_the_hold_before_the_first_dispatch():
    scheduler = PrintScheduler()
    with patch(
        "backend.app.services.print_scheduler.scheduled_drying.running_printer_ids", AsyncMock(return_value={3})
    ):
        await scheduler._seed_scheduled_drying_holds()
    assert 3 in scheduler._drying_in_progress and 3 in scheduler._scheduled_drying_printers


@pytest.mark.asyncio
async def test_disabled_auto_drying_leaves_a_scheduled_hold_alone():
    """Otherwise it "stops" (and logs) the scheduled printer every 30 s all night."""
    from unittest.mock import MagicMock

    scheduler = PrintScheduler()
    scheduler._drying_in_progress = {7: 1.0}
    scheduler._scheduled_drying_printers = {7}
    scheduler._stop_drying = AsyncMock()
    scheduler._get_bool_setting = AsyncMock(return_value=False)
    await scheduler._check_auto_drying(MagicMock(), [], set())
    scheduler._stop_drying.assert_not_awaited()
