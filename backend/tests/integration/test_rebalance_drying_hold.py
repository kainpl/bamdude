"""The rebalancer's «ready» knows the drying hold, as the router's rank does.

Under «Wait for drying to complete» (``queue_drying_block``) a printer whose
drying cycle the scheduler tracks holds its queue until the cycle ends. The
AutoQueue router ranks such a printer below an equally fit one that is free
(``find_eligible_printer``); the rebalancer computes the same «ready» for the
printers that RECEIVE moved work and for its forecast's first wave, and did not
know the hold - so it could move a job onto a printer that would keep it for
hours, and its forecast could place a job where the router does not.

The home P2S holds PLA/ABS and cannot print the PETG job; the P1S can.
"""

import time

import pytest

from backend.app.models.settings import Settings
from backend.app.services import queue_rebalance
from backend.app.services.print_scheduler import scheduler as print_scheduler
from backend.tests.integration.test_auto_queue_scheduler import _idle_status
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 - fixtures
    WHITE,
    _during_capture,
    _live,
    clean_spool,
    petg_farm,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _block_mode(db_session, value="true"):
    db_session.add(Settings(key="queue_drying_block", value=value))
    await db_session.commit()


def _hold(monkeypatch, printer_id: int) -> None:
    monkeypatch.setitem(print_scheduler._drying_in_progress, printer_id, time.monotonic())


async def _move(db_session, farm, status=None):
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        return await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)


async def test_a_printer_drying_and_holding_its_queue_receives_no_moved_work(
    db_session, printer_factory, tmp_path, monkeypatch
):
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    await _block_mode(db_session)
    _hold(monkeypatch, p1s.id)

    result = await _move(db_session, farm)

    assert result.converted == 0
    await db_session.refresh(farm.item)
    assert farm.item.rebalanced_at is None and farm.item.library_file_id == farm.home_file.id


async def test_with_the_block_off_a_drying_printer_receives_as_any_other(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """The print simply takes the printer, so nothing is held."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    await _block_mode(db_session, "false")
    _hold(monkeypatch, p1s.id)

    result = await _move(db_session, farm)

    assert (result.converted, result.skipped) == (1, [])


async def test_a_parked_timer_holds_nothing(db_session, printer_factory, tmp_path, monkeypatch):
    """A timer the printer holds without running it never ends: no wait behind it."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    await _block_mode(db_session)
    _hold(monkeypatch, p1s.id)
    status = dict(farm.status)
    parked = _idle_status(["PETG"], [WHITE])
    parked.raw_data["ams"][0].update(dry_time=720, dry_countdown_stalled=True)
    status[p1s.id] = parked

    result = await _move(db_session, farm, status=status)

    assert (result.converted, result.skipped) == (1, [])


async def test_a_receiver_that_starts_drying_during_the_copy_calls_the_move_off(
    db_session, printer_factory, tmp_path, monkeypatch, clean_spool
):
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    await _block_mode(db_session)

    async def start_drying():
        _hold(monkeypatch, p1s.id)

    _during_capture(monkeypatch, start_drying)

    result = await _move(db_session, farm)

    assert (result.converted, result.skipped) == (0, [(farm.item.id, "eligibility_changed")])
    await db_session.refresh(farm.item)
    assert farm.item.rebalanced_at is None, "a refused move must not start the cooldown"
