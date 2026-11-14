"""A session whose cleanup could not be proven is never followed by another process (G09 review F4).

``CameraAttempt`` names the end of an attempt whose ffmpeg it could not prove dead: ``cleanup_failed``. The
reconnect budget refused a redial after such an attempt only when the session had delivered NO frame; one
that delivered and then failed its cleanup got a new ffmpeg beside the one still holding the camera's single
connection. Since a frozen picture ends a connected session on purpose, that is the ordinary way a session
ends, not a corner.

No ffmpeg here: the session reader is scripted, and ends the way ``_stream_rtsp`` ends after
``CameraAttempt.__aexit__`` failed — the attempt's metric names ``cleanup_failed`` and the reader returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import time as real_time
from dataclasses import replace

import pytest

from backend.app.services import camera_metrics, external_camera as ec
from backend.app.services.camera_profiles import DEFAULT_PROFILE

BUILTIN = replace(DEFAULT_PROFILE, rtsp_reconnect_max=2, rtsp_reconnect_stable_seconds=30.0)


def picture(name) -> bytes:
    return b"\xff\xd8" + str(name).encode() + b"\xff\xd9"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


class Source:
    """Scripted sessions, one frame a second; the sessions listed in ``unproven`` fail their cleanup."""

    def __init__(self, monkeypatch, script, *, unproven=()) -> None:
        self.script = [list(session) for session in script]
        self.unproven = set(unproven)
        self.clock = Clock()
        self.stop_event = asyncio.Event()
        self.sessions = 0
        self.waits: list[float] = []

        async def session():
            if self.sessions >= len(self.script):
                self.stop_event.set()  # a run that redials past its script ends instead of looping
                self.sessions += 1
                return
            index = self.sessions
            self.sessions += 1
            try:
                for frame in self.script[index]:
                    self.clock.now += 1.0
                    yield frame
            finally:
                if index in self.unproven:
                    camera_metrics.current.get().end_reason = "cleanup_failed"

        # ``aclosing``: a reader closed mid-session (the freeze watch does that) runs its cleanup before
        # the stream loop reads the attempt's outcome, as ``_stream_rtsp``'s own ``async with`` does.
        async def rtsp(_url, _fps, *, on_process=None, profile=None):
            async with contextlib.aclosing(session()) as frames:
                async for frame in frames:
                    yield frame

        async def mjpeg(_url):
            async with contextlib.aclosing(session()) as frames:
                async for frame in frames:
                    await asyncio.sleep(0.02)  # the MJPEG path throttles on the loop clock
                    yield frame

        async def wait(delay, stop_event):
            self.waits.append(delay)
            return not (stop_event is not None and stop_event.is_set())

        monkeypatch.setattr(ec, "time", self.clock)
        monkeypatch.setattr(ec, "_stream_rtsp", rtsp)
        monkeypatch.setattr(ec, "_stream_mjpeg", mjpeg)
        monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)


async def _run(source: Source, camera_type: str, stream_id: str, *, profile=None):
    frames = 0
    stream = ec.generate_mjpeg_stream(
        "rtsp://127.0.0.1/live" if camera_type == "rtsp" else "http://127.0.0.1/live",
        camera_type,
        1000,
        rtsp_profile=profile,
        stop_event=source.stop_event,
        stream_id=stream_id,
    )
    try:
        async for _chunk in stream:
            frames += 1
    finally:
        await stream.aclose()
    return frames, camera_metrics._completed[stream_id]


MOVING = [picture(f"moving-{index}") for index in range(40)]
CASES = {"builtin-rtsp": ("rtsp", BUILTIN), "external-rtsp": ("rtsp", None), "external-mjpeg": ("mjpeg", None)}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_a_session_that_delivered_and_failed_its_cleanup_is_not_redialled(monkeypatch, case):
    camera_type, profile = CASES[case]
    source = Source(monkeypatch, [MOVING, MOVING], unproven={0})
    frames, metric = await _run(source, camera_type, f"unproven-{case}", profile=profile)
    assert source.sessions == 1, "a second process was started beside one not proven dead"
    assert source.waits == []
    assert frames == len(MOVING)
    assert metric.end_reason == "cleanup_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [BUILTIN, None], ids=["builtin", "external"])
async def test_a_frozen_session_that_failed_its_cleanup_is_not_redialled(monkeypatch, profile):
    """The watch ends a CONNECTED session; its ffmpeg not proven dead still holds the camera."""
    frozen = [picture("frozen")] * 500
    source = Source(monkeypatch, [MOVING + frozen, MOVING], unproven={0})
    _frames, metric = await _run(source, "rtsp", f"unproven-frozen-{'b' if profile else 'e'}", profile=profile)
    assert source.sessions == 1, "a second process was started beside one not proven dead"
    assert source.waits == []
    assert metric.end_reason == "cleanup_failed", "the end is the unproven cleanup, not the freeze it followed"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_a_session_with_a_proven_cleanup_is_redialled_as_before(monkeypatch, case):
    camera_type, profile = CASES[case]
    source = Source(monkeypatch, [MOVING, MOVING])
    frames, _metric = await _run(source, camera_type, f"proven-{case}", profile=profile)
    assert source.sessions == 3  # both scripted sessions, then the redial that ends the script
    assert frames == 2 * len(MOVING)
