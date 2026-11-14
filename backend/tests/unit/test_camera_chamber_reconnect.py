"""A chamber-image camera (A1 / A1 mini / P1P / P1S) is redialled on the same budget.

One read timeout or a printer-side close used to end the stream for everybody.
The producer now reconnects with the budget counted in a row, never opens a
socket before the previous one is closed, and stops on its cancellation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest

from backend.app import camera_worker
from backend.app.services import camera, camera_profiles, external_camera as ec
from backend.app.services.camera_profiles import DEFAULT_PROFILE

JPEG = b"\xff\xd8frame\xff\xd9"
HANG = object()


class Printer:
    """Scripted port-6000 sessions: ``None`` refuses the connect, a number is its frame count."""

    def __init__(self, monkeypatch, script, *, then=None, stable_seconds=0.0, budget=2):
        self.script = list(script)
        self.then = then
        self.connects = 0
        self.open = 0
        self.max_open = 0
        self.closed = 0
        self.waits: list[float] = []
        self.published: list[bytes] = []
        self.reading = asyncio.Event()
        profile = replace(DEFAULT_PROFILE, rtsp_reconnect_max=budget, rtsp_reconnect_stable_seconds=stable_seconds)

        async def connect(_ip, _code, _fps=5):
            plan = self.script[self.connects] if self.connects < len(self.script) else self.then
            self.connects += 1
            if plan is None:
                return None
            self.open += 1
            self.max_open = max(self.max_open, self.open)
            return _Reader(plan), _Writer(self)

        async def read(reader, timeout=10.0):
            if reader.plan is HANG:
                self.reading.set()
                await asyncio.Event().wait()
            if reader.plan <= 0:
                return None
            reader.plan -= 1
            return JPEG

        async def wait(delay, stop_event):
            self.waits.append(delay)
            return True

        monkeypatch.setattr(camera, "generate_chamber_image_stream", connect)
        monkeypatch.setattr(camera, "read_next_chamber_frame", read)
        monkeypatch.setattr(camera_profiles, "get_camera_profile", lambda _model: profile)
        monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)

    async def produce(self):
        await camera_worker._produce_chamber_frames("192.0.2.10", "code", 5, "A1", self.published.append)


class _Reader:
    def __init__(self, plan):
        self.plan = plan


class _Writer:
    def __init__(self, printer: Printer):
        self.printer = printer
        self.closing = False

    def close(self):
        self.closing = True

    async def wait_closed(self):
        assert self.closing
        await asyncio.sleep(0)
        self.printer.open -= 1
        self.printer.closed += 1


@pytest.mark.asyncio
async def test_chamber_stream_survives_more_drops_than_its_budget(monkeypatch):
    printer = Printer(monkeypatch, [2] * 6)
    await asyncio.wait_for(printer.produce(), 5)
    assert len(printer.published) == 12
    assert printer.max_open == 1
    assert printer.open == 0


@pytest.mark.asyncio
async def test_chamber_refused_redial_after_a_healthy_session_is_retried(monkeypatch):
    # healthy, a refused connect, healthy, a session without a frame, healthy
    printer = Printer(monkeypatch, [1, None, 1, 0, 1])
    await asyncio.wait_for(printer.produce(), 5)
    assert len(printer.published) == 3
    assert printer.max_open == 1


@pytest.mark.asyncio
async def test_chamber_stream_ends_after_its_budget_of_failures_in_a_row(monkeypatch, caplog):
    printer = Printer(monkeypatch, [1])
    with caplog.at_level(logging.WARNING, logger=camera_worker.logger.name):
        await asyncio.wait_for(printer.produce(), 5)
    assert printer.connects == 3  # the healthy session, then the budget of two
    assert len(printer.waits) == 2
    assert printer.open == 0
    text = caplog.text
    assert text.count("reconnecting") == 2
    assert "reason=connect_failed" in text and "consecutive_failures=2" in text


@pytest.mark.asyncio
async def test_chamber_sessions_that_never_hold_spend_the_budget(monkeypatch, caplog):
    printer = Printer(monkeypatch, [], then=1, stable_seconds=3600.0)
    with caplog.at_level(logging.WARNING, logger=camera_worker.logger.name):
        await asyncio.wait_for(printer.produce(), 5)
    assert printer.connects == 3
    assert "reason=retry_exhausted" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, 0])
async def test_chamber_stream_that_never_delivered_fails_at_once(monkeypatch, first):
    printer = Printer(monkeypatch, [first, 1])
    await asyncio.wait_for(printer.produce(), 5)
    assert (printer.connects, printer.waits, printer.published, printer.open) == (1, [], [], 0)


@pytest.mark.asyncio
async def test_cancelled_chamber_producer_closes_its_socket_and_does_not_redial(monkeypatch):
    printer = Printer(monkeypatch, [HANG, 1])
    task = asyncio.create_task(printer.produce())
    await asyncio.wait_for(printer.reading.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert (printer.connects, printer.open, printer.closed, printer.waits) == (1, 0, 1, [])


@pytest.mark.asyncio
async def test_cancel_during_the_chamber_backoff_opens_no_socket(monkeypatch):
    printer = Printer(monkeypatch, [1, 1])
    waiting = asyncio.Event()

    async def wait(delay, stop_event):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)
    task = asyncio.create_task(printer.produce())
    await asyncio.wait_for(waiting.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert (printer.connects, printer.open) == (1, 0)
