"""A camera stream's reconnect budget counts failures in a row (upstream 2315b8fb5).

A session that proved healthy — frames for the stable window — gives the budget
back, for the built-in RTSP camera and for an external RTSP / MJPEG one alike.
A session without a frame after the stream has delivered is one more failure of
that budget, retried with the backoff, never the end of the stream by itself.
A stream that never delivered a frame still fails at once.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from aiohttp import web

from backend.app.services import camera_metrics, external_camera as ec
from backend.app.services.camera_profiles import DEFAULT_PROFILE

JPEG = b"\xff\xd8frame\xff\xd9"
# A profile whose every delivering session counts as healthy / as never healthy.
HEALTHY = replace(DEFAULT_PROFILE, rtsp_reconnect_max=2, rtsp_reconnect_stable_seconds=0.0)
FLAKY = replace(DEFAULT_PROFILE, rtsp_reconnect_max=2, rtsp_reconnect_stable_seconds=3600.0)


class Source:
    """Scripted sessions: each entry is the number of frames that session delivers."""

    def __init__(self, monkeypatch, script, *, then=0):
        self.script = list(script)
        self.then = then
        self.sessions = 0
        self.waits: list[float] = []
        self.stop_event: asyncio.Event | None = None
        self.stop_at_wait: int | None = None

        async def rtsp(_url, _fps, *, on_process=None, profile=None):
            for frame in self._session():
                yield frame

        async def mjpeg(_url):
            for frame in self._session():
                # The MJPEG path throttles on the loop clock; step past one frame interval.
                await asyncio.sleep(0.02)
                yield frame

        async def wait(delay, stop_event):
            self.waits.append(delay)
            if stop_event is not None and len(self.waits) == self.stop_at_wait:
                stop_event.set()
            return not (stop_event is not None and stop_event.is_set())

        monkeypatch.setattr(ec, "_stream_rtsp", rtsp)
        monkeypatch.setattr(ec, "_stream_mjpeg", mjpeg)
        monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)

    def _session(self):
        frames = self.script[self.sessions] if self.sessions < len(self.script) else self.then
        self.sessions += 1
        return [JPEG] * frames


async def _run(camera_type, stream_id, *, profile=None, stop_event=None, limit=1000):
    """Drain the stream to its own end (or ``limit`` frames); return frames and its metric."""
    frames = 0
    stream = ec.generate_mjpeg_stream(
        "rtsp://127.0.0.1/live" if camera_type == "rtsp" else "http://127.0.0.1/live",
        camera_type,
        1000,
        rtsp_profile=profile,
        stop_event=stop_event,
        stream_id=stream_id,
    )
    try:
        async for _chunk in stream:
            frames += 1
            if frames >= limit:
                break
    finally:
        await stream.aclose()
    return frames, camera_metrics._completed[stream_id]


@pytest.fixture(autouse=True)
def _external_sessions_are_healthy(monkeypatch):
    # An external camera has no profile: its stable window is the module's own.
    monkeypatch.setattr(ec, "_EXTERNAL_RECONNECT_STABLE_SECONDS", 0.0, raising=False)


# ---------------------------------------------------------------- built-in RTSP


@pytest.mark.asyncio
async def test_builtin_budget_is_given_back_by_every_healthy_session(monkeypatch):
    """Ten drops, each after a healthy session, on a budget of two: the stream lives."""
    source = Source(monkeypatch, [1] * 10)
    frames, metric = await _run("rtsp", "budget-builtin-healthy", profile=HEALTHY)
    assert frames == 10
    assert source.sessions > HEALTHY.rtsp_reconnect_max + 1


@pytest.mark.asyncio
async def test_builtin_sessions_that_never_hold_spend_the_budget(monkeypatch):
    """A source that delivers and drops before the stable window is not forgiven."""
    source = Source(monkeypatch, [], then=1)
    frames, metric = await _run("rtsp", "budget-builtin-flaky", profile=FLAKY)
    assert source.sessions == FLAKY.rtsp_reconnect_max + 1
    assert frames == source.sessions
    assert metric.end_reason == "retry_exhausted"
    assert metric.consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_refused_redial_after_a_healthy_session_is_retried(monkeypatch):
    """One frameless session between two that deliver does not end the stream."""
    source = Source(monkeypatch, [1, 0, 1, 0, 1])
    frames, metric = await _run("rtsp", "budget-builtin-refused", profile=HEALTHY)
    assert frames == 3
    # the five scripted sessions, then the budget of two frameless redials
    assert source.sessions == 7


@pytest.mark.asyncio
async def test_frameless_sessions_in_a_row_end_the_stream_after_the_budget(monkeypatch):
    source = Source(monkeypatch, [1])
    frames, metric = await _run("rtsp", "budget-builtin-dead", profile=HEALTHY)
    assert frames == 1
    # the healthy session, then the whole budget of frameless redials
    assert source.sessions == 1 + HEALTHY.rtsp_reconnect_max
    assert len(source.waits) == HEALTHY.rtsp_reconnect_max
    assert metric.end_reason == "connect_failed"
    assert metric.consecutive_failures == HEALTHY.rtsp_reconnect_max


@pytest.mark.asyncio
async def test_a_stream_that_never_delivered_fails_at_once(monkeypatch):
    source = Source(monkeypatch, [0, 1])
    frames, metric = await _run("rtsp", "budget-builtin-first", profile=HEALTHY)
    assert (frames, source.sessions, source.waits) == (0, 1, [])
    assert metric.end_reason == "connect_failed"
    assert metric.consecutive_failures == 1


@pytest.mark.asyncio
async def test_stop_during_the_wait_after_a_refused_redial_opens_no_session(monkeypatch):
    source = Source(monkeypatch, [1, 0, 1])
    stop = asyncio.Event()
    source.stop_at_wait = 2
    frames, metric = await _run("rtsp", "budget-builtin-stop", profile=HEALTHY, stop_event=stop)
    assert (frames, source.sessions) == (1, 2)
    assert metric.end_reason == "client_disconnected"


# ------------------------------------------------------- external RTSP and MJPEG


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_type", ["rtsp", "mjpeg"])
async def test_external_camera_has_no_lifetime_cap(monkeypatch, camera_type):
    """Five healthy sessions that each dropped: an external camera keeps streaming."""
    source = Source(monkeypatch, [1] * 6)
    frames, metric = await _run(camera_type, f"budget-external-{camera_type}")
    assert frames == 6
    assert source.sessions > 6


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_type", ["rtsp", "mjpeg"])
async def test_external_refused_redial_after_a_healthy_session_is_retried(monkeypatch, camera_type):
    source = Source(monkeypatch, [1, 0, 1])
    frames, metric = await _run(camera_type, f"budget-external-refused-{camera_type}")
    assert frames == 2
    # healthy, refused, healthy, then three frameless redials spend the budget
    assert source.sessions == 6
    assert metric.end_reason == "connect_failed"
    assert metric.consecutive_failures == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_type", ["rtsp", "mjpeg"])
async def test_external_sessions_that_never_hold_spend_the_budget(monkeypatch, camera_type):
    monkeypatch.setattr(ec, "_EXTERNAL_RECONNECT_STABLE_SECONDS", 3600.0, raising=False)
    source = Source(monkeypatch, [], then=1)
    frames, metric = await _run(camera_type, f"budget-external-flaky-{camera_type}")
    assert (frames, source.sessions) == (4, 4)
    assert metric.end_reason == "retry_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("camera_type", ["rtsp", "mjpeg"])
async def test_external_stream_that_never_delivered_fails_at_once(monkeypatch, camera_type):
    source = Source(monkeypatch, [0, 1])
    frames, metric = await _run(camera_type, f"budget-external-first-{camera_type}")
    assert (frames, source.sessions, source.waits) == (0, 1, [])
    assert metric.end_reason == "connect_failed"


@pytest.mark.asyncio
async def test_external_mjpeg_wait_is_interrupted_by_stop(monkeypatch):
    source = Source(monkeypatch, [1, 1])
    stop = asyncio.Event()
    source.stop_at_wait = 1
    frames, metric = await _run("mjpeg", "budget-external-mjpeg-stop", stop_event=stop)
    assert (frames, source.sessions) == (1, 1)


# ------------------------------------------------- no redial after cancellation


@asynccontextmanager
async def _mjpeg_endpoint(connections: list[int]):
    """A loopback MJPEG source: one frame, then the body stays open."""

    async def handler(request):
        connections.append(1)
        response = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
        await response.prepare(request)
        await response.write(JPEG)
        await asyncio.Event().wait()

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/live"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_cancelled_mjpeg_producer_ends_without_another_session(monkeypatch):
    """The producer's cancel lands inside the session's read; nothing may redial after it."""
    connections: list[int] = []
    waits: list[float] = []
    got_frame = asyncio.Event()

    async def wait(delay, stop_event):
        waits.append(delay)
        return True

    monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)

    async def producer(url):
        async for _chunk in ec.generate_mjpeg_stream(url, "mjpeg", 1000, stream_id="budget-mjpeg-cancel"):
            got_frame.set()

    async with _mjpeg_endpoint(connections) as url:
        task = asyncio.create_task(producer(url))
        await asyncio.wait_for(got_frame.wait(), 5)
        await asyncio.sleep(0.05)  # parked in the body read
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=1)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert done, "a cancelled producer kept running"
        assert task.cancelled()
    assert connections == [1]
    assert waits == []
    assert camera_metrics._completed["budget-mjpeg-cancel"].end_reason == "client_disconnected"
