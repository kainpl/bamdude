"""The dispatch runner never writes over a file it was told to leave alone (G10 B, review B1/B2).

Driven through the real ``_run_reprint_archive`` with the hardware mocked
(``run_reprint``): what matters is not that a helper refuses, but that the
runner does not go on to the upload afterwards.
"""

from __future__ import annotations

import pytest

from backend.app.services import background_dispatch
from backend.app.services.background_dispatch import UploadFailed
from backend.tests.integration.test_reprint_creates_new_archive import run_reprint

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def run(db_session, test_engine, tmp_path, monkeypatch, printer_factory):
    """Run the reprint runner; returns its doubles and the refusal it ended in, if any."""
    monkeypatch.setattr(background_dispatch._FileGate, "OBSERVE_TIMEOUT", 0.3)
    monkeypatch.setattr(background_dispatch._FileGate, "_OBSERVE_POLL", 0.05)

    async def go(**kwargs):
        doubles: dict = {"refusal": None}
        try:
            await run_reprint(
                db_session, test_engine, tmp_path, monkeypatch, printer_factory, doubles=doubles, **kwargs
            )
        except UploadFailed as refusal:
            doubles["refusal"] = refusal
        return doubles

    return go


async def test_a_free_printer_is_cleared_and_uploaded_to(run):
    ran = await run()
    assert ran["refusal"] is None
    assert ran["upload"].await_count == 1
    # The upload carries the same gate, and on a free printer it allows the first byte.
    assert ran["upload"].await_args.kwargs["may_run"]() is True
    assert ran["delete"].await_count >= 1


async def test_a_print_that_begins_while_the_clear_waits_is_never_uploaded_over(run):
    """B starts under the job's own file name while J waits for the file channel."""

    def b_starts(mqtt):
        mqtt.state.state = "PREPARE"
        mqtt.state.subtask_name = "source"
        mqtt.state.gcode_file = "/sdcard/source.3mf"

    ran = await run(during_clear=b_starts)
    assert "belongs to another print" in str(ran["refusal"])
    assert ran["upload"].await_count == 0


async def test_a_refusal_is_not_forgotten_when_the_printer_looks_free_again(run):
    """The delete was refused; by the time anybody re-reads the guard, B has already ended."""
    seen = {"calls": 0}

    def b_starts_and_ends(mqtt):
        seen["calls"] += 1
        mqtt.state.state = "PREPARE"
        mqtt.state.subtask_name = "source"
        mqtt.state.gcode_file = "/sdcard/source.3mf"
        seen["mqtt"] = mqtt

    original = background_dispatch._FileGate.raise_if_refused

    def free_again_then_raise(self):
        if "mqtt" in seen:
            seen["mqtt"].state.state = "FINISH"  # a re-read of the guard would now say "safe"
        return original(self)

    background_dispatch._FileGate.raise_if_refused = free_again_then_raise
    try:
        ran = await run(during_clear=b_starts_and_ends)
    finally:
        background_dispatch._FileGate.raise_if_refused = original
    assert ran["refusal"] is not None
    assert ran["upload"].await_count == 0


async def test_a_printer_that_goes_silent_while_the_clear_waits_is_never_uploaded_to(run):
    def goes_silent(mqtt):
        mqtt.state.connected = False

    ran = await run(during_clear=goes_silent)
    assert "state is not known" in str(ran["refusal"])
    assert ran["upload"].await_count == 0


async def test_a_printer_that_never_reports_is_neither_cleared_nor_uploaded_to(run):
    """A cached state, or none at all, grants nothing: the job stops before its first command."""
    ran = await run(printer_state=None)
    assert "state is not known" in str(ran["refusal"])
    assert ran["delete"].await_count == 0
    assert ran["upload"].await_count == 0


async def test_a_printer_that_reconnects_in_time_is_waited_for(run, monkeypatch):
    """Silent at first, reporting a moment later: the job waits instead of failing."""
    import time

    monkeypatch.setattr(background_dispatch._FileGate, "OBSERVE_TIMEOUT", 5.0)
    real_sleep = background_dispatch.asyncio.sleep
    woke = {}

    async def report_after_first_poll(delay):
        from backend.app.services.printer_manager import printer_manager

        for client in list(printer_manager._clients.values()):
            if getattr(client, "serial_number", "") == "SYNTHETIC" and "done" not in woke:
                client.state.state = "FINISH"
                client._last_message_time = time.time()
                woke["done"] = True
        await real_sleep(0)

    monkeypatch.setattr(background_dispatch.asyncio, "sleep", report_after_first_poll)
    ran = await run(printer_state=None)
    assert ran["refusal"] is None
    assert ran["upload"].await_count == 1
