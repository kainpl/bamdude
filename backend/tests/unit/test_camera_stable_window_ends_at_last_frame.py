"""A session's health is measured to its LAST FRAME, not to the end of the silence after it (G09 review M4).

The reconnect budget gives its retries back to a session that held for the stable window. The window was
measured to the moment the session ENDED — which, for a source that goes silent, is the end of the 30 s read
timeout. A camera that sends one frame per connection and then nothing was therefore "stable" every time and
was redialled for as long as anybody watched, never spending the budget that exists to end such a stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import time as real_time
from dataclasses import replace

import pytest

from backend.app import camera_worker
from backend.app.services import camera, camera_metrics, camera_profiles, external_camera as ec
from backend.app.services.camera_profiles import DEFAULT_PROFILE

JPEG = b"\xff\xd8frame\xff\xd9"
BUILTIN = replace(DEFAULT_PROFILE, rtsp_reconnect_max=2, rtsp_reconnect_stable_seconds=30.0)
READ_TIMEOUT = 30.0
# Far more than any budget: a run that reaches it was being redialled without end.
RUNAWAY = 12


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


class Source:
    """Every session: ``frames`` pictures a second apart, then ``silence`` seconds before the reader gives up."""

    def __init__(self, monkeypatch, *, frames: int, silence: float) -> None:
        self.clock = Clock()
        self.stop_event = asyncio.Event()
        self.sessions = 0
        self.waits: list[float] = []

        async def session():
            self.sessions += 1
            if self.sessions > RUNAWAY:
                self.stop_event.set()
                return
            for index in range(frames):
                self.clock.now += 1.0
                yield b"\xff\xd8" + f"{self.sessions}-{index}".encode() + b"\xff\xd9"
            self.clock.now += silence

        async def rtsp(_url, _fps, *, on_process=None, profile=None):
            async with contextlib.aclosing(session()) as pictures:
                async for frame in pictures:
                    yield frame

        async def mjpeg(_url):
            async with contextlib.aclosing(session()) as pictures:
                async for frame in pictures:
                    # The MJPEG path throttles on the loop clock; twice its 1 ms interval keeps every frame
                    # published. The stable-window verdict reads the faked clock, not this pacing.
                    await asyncio.sleep(0.002)
                    yield frame

        async def wait(delay, stop_event):
            self.waits.append(delay)
            return not (stop_event is not None and stop_event.is_set())

        monkeypatch.setattr(ec, "time", self.clock)
        monkeypatch.setattr(ec, "_stream_rtsp", rtsp)
        monkeypatch.setattr(ec, "_stream_mjpeg", mjpeg)
        monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)


async def _run(source: Source, camera_type: str, stream_id: str, *, profile=None):
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
            pass
    finally:
        await stream.aclose()
    return camera_metrics._completed[stream_id]


# (camera type, profile, reconnects the budget allows in a row)
CASES = {"builtin-rtsp": ("rtsp", BUILTIN, 2), "external-rtsp": ("rtsp", None, 3), "external-mjpeg": ("mjpeg", None, 3)}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_one_frame_and_then_silence_is_not_a_stable_session(monkeypatch, case):
    camera_type, profile, budget = CASES[case]
    source = Source(monkeypatch, frames=1, silence=READ_TIMEOUT)
    metric = await _run(source, camera_type, f"stable-silent-{case}", profile=profile)
    assert source.sessions == 1 + budget, "the silence after the last frame was counted as health"
    assert metric.end_reason == "retry_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_frames_for_the_whole_window_still_give_the_budget_back(monkeypatch, case):
    camera_type, profile, _budget = CASES[case]
    source = Source(monkeypatch, frames=40, silence=READ_TIMEOUT)
    await _run(source, camera_type, f"stable-held-{case}", profile=profile)
    assert source.sessions > RUNAWAY, "a session that delivered for the stable window was not forgiven"


@pytest.mark.asyncio
async def test_a_chamber_camera_that_sends_one_frame_and_goes_silent_spends_its_budget(monkeypatch):
    """The A1/P1 producer keeps its own loop on the same budget; here the clock is the real one."""
    stable, silence = 0.05, 0.15
    connects = 0
    profile = replace(DEFAULT_PROFILE, rtsp_reconnect_max=2, rtsp_reconnect_stable_seconds=stable)

    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(_ip, _code, _fps=5):
        nonlocal connects
        connects += 1
        return (None if connects > RUNAWAY else [1]), Writer()

    async def read(reader, timeout=10.0):
        if reader is None:
            raise asyncio.CancelledError  # the runaway guard: end the producer
        if reader[0]:
            reader[0] -= 1
            return JPEG
        await asyncio.sleep(silence)  # the read timeout of a silent source
        return None

    async def wait(_delay, _stop_event):
        return True

    monkeypatch.setattr(camera, "generate_chamber_image_stream", connect)
    monkeypatch.setattr(camera, "read_next_chamber_frame", read)
    monkeypatch.setattr(camera_profiles, "get_camera_profile", lambda _model: profile)
    monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)

    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(
            camera_worker._produce_chamber_frames("192.0.2.10", "code", 5, "A1", lambda _f: None), 20
        )
    assert connects == 3, "the silence after the last frame was counted as health"
