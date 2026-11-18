"""The connect-edge sweep waits for the analysis service of a starting process.

A print that ended while BamDude was down is closed by the sweep that runs on the printer's first
status. Its filament is booked from the 3MF analysis, and the sweep is allowed one attempt at it.
Printers reconnect some twenty seconds before the lifespan starts the analysis service, so that one
attempt always met "analysis service unavailable": the archive was closed as completed and nothing
was charged to the spools (farm log, 2026-11-18: three prints that ended overnight). The sweep now
waits, bounded, for the service's first start, and then reads the printer's state again -- the status
it was called with may be half a minute old by then.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.services import analysis_runtime, print_reconciliation
from backend.app.services.printer_manager import printer_manager


@pytest.fixture
def startup(monkeypatch):
    """A process whose lifespan has announced the service and not started it yet."""
    monkeypatch.setattr(analysis_runtime, "runtime", None)
    monkeypatch.setattr(analysis_runtime, "_start_expected", True)
    monkeypatch.setattr(analysis_runtime, "_started_once", False)


@pytest.fixture
def sweeps(monkeypatch):
    calls: list[tuple] = []

    async def _sweep(printer_id, live_state, live_file, subtask_id="", subtask_name="", *, printer_life=None):
        calls.append((live_state, live_file, subtask_id, subtask_name))

    monkeypatch.setattr(print_reconciliation, "_sweep_printer_prints", _sweep)
    return calls


def _client(monkeypatch, *, connected=True, state="FINISH", gcode_file="a.3mf", subtask_id="1", subtask_name="a"):
    client = SimpleNamespace(
        state=SimpleNamespace(
            connected=connected, state=state, gcode_file=gcode_file, subtask_id=subtask_id, subtask_name=subtask_name
        )
    )
    monkeypatch.setattr(printer_manager, "get_client", lambda printer_id: client)
    return client


def test_nothing_is_pending_in_a_process_that_announced_no_start(monkeypatch):
    monkeypatch.setattr(analysis_runtime, "_start_expected", False)
    monkeypatch.setattr(analysis_runtime, "_started_once", False)

    assert analysis_runtime.service_start_pending() is False


async def test_the_wait_ends_when_the_first_start_has_finished(startup, monkeypatch):
    waiter = asyncio.create_task(analysis_runtime.wait_for_first_start(5.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    monkeypatch.setattr(analysis_runtime, "_started_once", True)

    assert await waiter is True


async def test_the_wait_is_bounded(startup):
    assert await analysis_runtime.wait_for_first_start(0.05) is False


async def test_a_sweep_outside_start_up_runs_at_once_with_its_own_status(monkeypatch, sweeps):
    monkeypatch.setattr(analysis_runtime, "_start_expected", False)
    _client(monkeypatch, state="RUNNING")

    await print_reconciliation.reconcile_printer_prints(7, "FINISH", "a.3mf", "1", "a")

    assert sweeps == [("FINISH", "a.3mf", "1", "a")]


async def test_a_sweep_during_start_up_waits_for_the_service(startup, monkeypatch, sweeps):
    _client(monkeypatch)
    task = asyncio.create_task(print_reconciliation.reconcile_printer_prints(7, "FINISH", "a.3mf", "1", "a"))
    await asyncio.sleep(0.05)
    assert sweeps == []

    monkeypatch.setattr(analysis_runtime, "_started_once", True)
    await task

    assert sweeps == [("FINISH", "a.3mf", "1", "a")]


async def test_after_the_wait_the_sweep_uses_the_printers_state_of_now(startup, monkeypatch, sweeps):
    _client(monkeypatch, state="RUNNING", gcode_file="b.3mf", subtask_id="2", subtask_name="b")
    task = asyncio.create_task(print_reconciliation.reconcile_printer_prints(7, "FINISH", "a.3mf", "1", "a"))
    await asyncio.sleep(0.05)
    monkeypatch.setattr(analysis_runtime, "_started_once", True)
    await task

    assert sweeps == [("RUNNING", "b.3mf", "2", "b")]


async def test_a_printer_that_dropped_during_the_wait_is_swept_on_its_next_status(startup, monkeypatch, sweeps):
    client = _client(monkeypatch, connected=False)
    client._startup_reconcile_done = True
    task = asyncio.create_task(print_reconciliation.reconcile_printer_prints(7, "FINISH", "a.3mf", "1", "a"))
    await asyncio.sleep(0.05)
    monkeypatch.setattr(analysis_runtime, "_started_once", True)
    await task

    assert sweeps == []
    assert client._startup_reconcile_done is False  # the one-shot is handed back
