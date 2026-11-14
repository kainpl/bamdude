"""An RTSP stream that keeps repeating one frame is noticed and redialled (upstream e0149ae4a, 4a1f602bf).

A source can stop producing new pictures while its session stays up and keeps
delivering byte-identical frames.  The worker's stream loop ends such a session
and redials through its ordinary reconnect budget; a picture that really does
not change is told apart by what the redial shows.  No ffmpeg here: the session
reader is replaced by a scripted one on a scripted clock.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time as real_time
from dataclasses import replace

import pytest

from backend.app.services import camera_metrics, external_camera as ec
from backend.app.services.camera_profiles import DEFAULT_PROFILE
from backend.app.services.camera_worker_logging import WorkerLogHandler, decode_worker_log, set_camera_log_secrets

# Built-in camera: a budget of two reconnects in a row, the real 30 s stable window.
BUILTIN = replace(DEFAULT_PROFILE, rtsp_reconnect_max=2, rtsp_reconnect_stable_seconds=30.0)
URL = "rtsp://viewer:camera-password@127.0.0.1/live"


def picture(name) -> bytes:
    return b"\xff\xd8" + str(name).encode() + b"\xff\xd9"


def moving(seconds: int, tag: str) -> list[bytes]:
    """One new picture a second."""
    return [picture(f"{tag}-{index}") for index in range(seconds)]


def still(seconds: int, name) -> list[bytes]:
    """The same bytes once a second."""
    return [picture(name)] * seconds


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


class Source:
    """Scripted RTSP sessions, one frame a second on a scripted clock.

    A session whose script runs out ends like an ordinary drop.  When every
    scripted session has been opened the source stops the stream, so a run ends
    without spending the budget on a dead source.
    """

    def __init__(self, monkeypatch, script, *, frameless_after=False) -> None:
        self.script = [list(session) for session in script]
        self.clock = Clock()
        self.stop_event = asyncio.Event()
        self.frameless_after = frameless_after
        self.sessions = 0
        self.consumed: list[int] = []
        self.open = 0
        self.most_open = 0
        self.closed = 0
        self.waits: list[float] = []
        self.stop_at: tuple[int, int] | None = None  # (session index, frame index) at which a stop is requested

        async def rtsp(_url, _fps, *, on_process=None, profile=None):
            if self.sessions >= len(self.script):
                if not self.frameless_after:
                    self.stop_event.set()
                else:
                    self.sessions += 1
                return
            index = self.sessions
            self.sessions += 1
            self.consumed.append(0)
            self.open += 1
            self.most_open = max(self.most_open, self.open)
            try:
                for number, frame in enumerate(self.script[index]):
                    self.clock.now += 1.0
                    if self.stop_at == (index, number):
                        self.stop_event.set()
                    self.consumed[index] += 1
                    yield frame
            finally:
                self.open -= 1
                self.closed += 1

        async def wait(delay, stop_event):
            self.waits.append(delay)
            return not (stop_event is not None and stop_event.is_set())

        monkeypatch.setattr(ec, "time", self.clock)
        monkeypatch.setattr(ec, "_stream_rtsp", rtsp)
        monkeypatch.setattr(ec, "_wait_for_rtsp_retry", wait)


async def _run(source: Source, stream_id: str, *, profile=BUILTIN):
    frames = 0
    stream = ec.generate_mjpeg_stream(
        URL, "rtsp", 1000, rtsp_profile=profile, stop_event=source.stop_event, stream_id=stream_id
    )
    try:
        async for _chunk in stream:
            frames += 1
    finally:
        await stream.aclose()
    return frames, camera_metrics._completed[stream_id]


def _freeze_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "stopped changing" in record.getMessage()]


# ------------------------------------------------------------- the watch itself


@pytest.mark.asyncio
async def test_a_frozen_source_is_redialled_after_the_limit(monkeypatch):
    """Forty live seconds, then one picture for ever: the session is ended and the next one opened."""
    frozen = picture("moving-39")
    source = Source(monkeypatch, [moving(40, "moving") + [frozen] * 500, moving(5, "after")])
    frames, metric = await _run(source, "frozen-redial")
    assert source.sessions == 2
    # 40 pictures, then the repeats up to the first one past the 20 s limit
    assert source.consumed == [40 + 21, 5]
    # the repeat that tripped the limit is not published
    assert frames == 40 + 20 + 5
    assert source.most_open == 1, "the frozen session must be closed before the redial"
    assert metric.snapshot()["frozen_restarts_total"] == 1
    assert metric.snapshot()["still_rechecks_total"] == 0
    assert metric.end_reason == "client_disconnected"


@pytest.mark.asyncio
async def test_a_changing_picture_is_never_redialled(monkeypatch):
    source = Source(monkeypatch, [moving(2000, "live")])
    frames, metric = await _run(source, "frozen-never")
    assert (source.sessions, frames) == (1, 2000)
    assert metric.snapshot()["frozen_restarts_total"] == 0


@pytest.mark.asyncio
async def test_frames_repeated_for_less_than_the_limit_are_left_alone(monkeypatch):
    """A source slower than the output rate repeats each picture for a while: that is not a freeze."""
    script = [frame for index in range(30) for frame in still(15, f"slow-{index}")]
    source = Source(monkeypatch, [script])
    frames, metric = await _run(source, "frozen-slow-output")
    assert (source.sessions, frames) == (1, len(script))


@pytest.mark.asyncio
async def test_a_still_scene_is_rechecked_only_at_the_long_interval(monkeypatch):
    """The redial shows the same picture: the camera is alive and its scene is still."""
    dark = picture("dark-chamber")
    source = Source(
        monkeypatch,
        [
            [dark] * 2000,  # ended at the 20 s limit
            [dark] * 2000,  # the same picture again: a still scene, ended only at 300 s
            [dark] * 2000,  # and again
            moving(10, "lights-on") + still(2000, "lights-on-9"),  # a new picture: back to the 20 s limit
            moving(3, "end"),
        ],
    )
    frames, metric = await _run(source, "frozen-still")
    assert source.consumed == [1 + 21, 1 + 301, 1 + 301, 10 + 21, 3]
    assert source.most_open == 1
    assert metric.snapshot()["frozen_restarts_total"] == 2
    assert metric.snapshot()["still_rechecks_total"] == 2


@pytest.mark.asyncio
async def test_a_slow_external_camera_is_redialled_once_and_then_left_alone(monkeypatch):
    """No profile: the limit grows to three times the longest gap the camera has shown between pictures."""
    first = [frame for index in range(4) for frame in still(40, f"a-{index}")]
    second = [frame for index in range(30) for frame in still(40, f"b-{index}")]
    source = Source(monkeypatch, [first, second])
    frames, metric = await _run(source, "frozen-slow-external", profile=None)
    # ended once, 21 s into its first picture; the second session's 40 s gaps are within 3 x 21 s
    assert source.consumed == [1 + 21, len(second)]
    assert metric.snapshot()["frozen_restarts_total"] == 1


def test_the_adaptive_limit_never_exceeds_the_still_scene_interval():
    watch = ec._FreezeWatch(adaptive=True)
    watch.start_session()
    assert watch.observe(picture("a"), 0.0) is False
    assert watch.observe(picture("b"), 250.0) is False  # a 250 s gap: three times that is far past the cap
    assert watch.observe(picture("b"), 250.0 + ec._RTSP_STILL_RECHECK_SECONDS) is False
    assert watch.observe(picture("b"), 251.0 + ec._RTSP_STILL_RECHECK_SECONDS) is True


def test_a_builtin_camera_keeps_the_fixed_limit():
    watch = ec._FreezeWatch(adaptive=False)
    watch.start_session()
    watch.observe(picture("a"), 0.0)
    watch.observe(picture("b"), 100.0)
    assert watch.observe(picture("b"), 100.0 + ec._RTSP_FROZEN_SECONDS) is False
    assert watch.observe(picture("b"), 101.0 + ec._RTSP_FROZEN_SECONDS) is True


# ------------------------------------------------------------------ the budget


@pytest.mark.asyncio
async def test_a_source_that_freezes_after_every_redial_spends_the_budget(monkeypatch):
    """One new picture per connection and then nothing new: not redialled for ever."""
    source = Source(monkeypatch, [still(2000, f"session-{index}") for index in range(10)])
    frames, metric = await _run(source, "frozen-budget-spent")
    assert source.sessions == BUILTIN.rtsp_reconnect_max + 1
    assert metric.end_reason == "source_frozen"
    assert metric.snapshot()["frozen_restarts_total"] == BUILTIN.rtsp_reconnect_max + 1


@pytest.mark.asyncio
async def test_a_healthy_stream_that_freezes_now_and_then_keeps_its_budget(monkeypatch):
    """Each session showed a changing picture for the stable window before it froze."""
    script = [moving(60, f"s{index}") + still(2000, f"s{index}-59") for index in range(8)]
    source = Source(monkeypatch, script)
    frames, metric = await _run(source, "frozen-budget-kept")
    assert source.sessions == 8
    assert metric.end_reason == "client_disconnected"


@pytest.mark.asyncio
async def test_a_still_scene_never_spends_the_budget(monkeypatch):
    dark = picture("dark")
    source = Source(monkeypatch, [[dark] * 2000 for _ in range(9)])
    frames, metric = await _run(source, "frozen-budget-still")
    assert source.sessions == 9
    assert metric.end_reason == "client_disconnected"


@pytest.mark.asyncio
async def test_a_frozen_picture_before_the_stable_window_is_not_a_healthy_session(monkeypatch):
    """Ten live seconds and twenty frozen ones are thirty seconds of frames, not thirty healthy seconds."""
    script = [moving(10, f"s{index}") + still(2000, f"s{index}-9") for index in range(10)]
    source = Source(monkeypatch, script)
    frames, metric = await _run(source, "frozen-budget-short")
    assert source.sessions == BUILTIN.rtsp_reconnect_max + 1
    assert metric.end_reason == "source_frozen"


# ------------------------------------------------------------ stop and cancel


@pytest.mark.asyncio
async def test_a_stop_while_the_picture_is_frozen_opens_no_new_session(monkeypatch):
    source = Source(monkeypatch, [still(2000, "frozen"), moving(5, "never")])
    source.stop_at = (0, 21)  # the frame that trips the limit
    frames, metric = await _run(source, "frozen-stop")
    assert (source.sessions, source.closed) == (1, 1)
    assert source.waits == []
    assert metric.end_reason == "client_disconnected"


@pytest.mark.asyncio
async def test_a_cancel_while_the_picture_is_frozen_closes_the_session_and_opens_nothing(monkeypatch):
    source = Source(monkeypatch, [still(2000, "frozen"), moving(5, "never")])
    seen = asyncio.Event()

    async def producer():
        async for _chunk in ec.generate_mjpeg_stream(
            URL, "rtsp", 1000, rtsp_profile=BUILTIN, stream_id="frozen-cancel"
        ):
            seen.set()
            await asyncio.sleep(0.01)  # parked between two repeats, well inside the limit

    task = asyncio.create_task(producer())
    await asyncio.wait_for(seen.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    for _ in range(50):
        if source.closed:
            break
        await asyncio.sleep(0.01)
    assert (source.sessions, source.closed, source.open) == (1, 1, 0)
    assert camera_metrics._completed["frozen-cancel"].end_reason == "client_disconnected"


# ------------------------------------------------------- what the parent is told


@pytest.mark.asyncio
async def test_one_log_line_per_freeze_never_one_per_frame(monkeypatch, caplog):
    frozen = picture("moving-39")
    source = Source(monkeypatch, [moving(40, "moving") + [frozen] * 500, moving(5, "after")])
    with caplog.at_level(logging.INFO, logger=ec.logger.name):
        await _run(source, "frozen-log")
    lines = _freeze_lines(caplog)
    assert len(lines) == 1
    assert "21 identical frames" in lines[0] and "21s" in lines[0]
    assert "new picture" in lines[0]
    # the redial is reported by that line; the one "reconnecting" line is the second session's own drop
    assert len([r for r in caplog.records if "reconnecting" in r.getMessage()]) == 1
    assert "camera-password" not in caplog.text and "127.0.0.1" not in caplog.text


@pytest.mark.asyncio
async def test_a_still_scene_is_reported_once_not_at_every_recheck(monkeypatch, caplog):
    dark = picture("dark")
    source = Source(monkeypatch, [[dark] * 2000 for _ in range(6)])
    with caplog.at_level(logging.INFO, logger=ec.logger.name):
        frames, metric = await _run(source, "frozen-log-still")
    lines = _freeze_lines(caplog)
    assert len(lines) == 1
    assert "same picture" in lines[0] and "300s" in lines[0]
    assert metric.snapshot()["still_rechecks_total"] == 5
    assert not [r for r in caplog.records if "reconnecting" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_redial_that_gets_no_frame_is_reported_with_the_freeze(monkeypatch, caplog):
    source = Source(monkeypatch, [moving(40, "live") + still(2000, "live-39")], frameless_after=True)
    with caplog.at_level(logging.INFO, logger=ec.logger.name):
        frames, metric = await _run(source, "frozen-log-dead")
    lines = _freeze_lines(caplog)
    assert len(lines) == 1
    assert "no frame" in lines[0]
    assert metric.end_reason == "connect_failed"


@pytest.mark.asyncio
async def test_the_freeze_line_reaches_the_parent_without_the_camera_address(monkeypatch):
    """Through the worker's own log handler and the parent's decoder, as a worker record travels."""
    source = Source(monkeypatch, [still(2000, "frozen"), moving(5, "after")])
    output = io.StringIO()
    monkeypatch.setattr("sys.stderr", output)
    handler = WorkerLogHandler()
    ec.logger.addHandler(handler)
    previous_level = ec.logger.level
    ec.logger.setLevel(logging.INFO)
    set_camera_log_secrets(URL)
    try:
        await _run(source, "frozen-parent-log")
    finally:
        set_camera_log_secrets()
        ec.logger.removeHandler(handler)
        ec.logger.setLevel(previous_level)
    decoded = [decode_worker_log(line.encode()) for line in output.getvalue().splitlines(keepends=True)]
    freeze = [record for record in decoded if record is not None and "stopped changing" in record[2]]
    assert len(freeze) == 1
    level, name, message = freeze[0]
    assert (level, name) == (logging.WARNING, ec.logger.name)
    assert "camera-password" not in output.getvalue() and "rtsp://" not in output.getvalue()


@pytest.mark.asyncio
async def test_a_completed_stream_carries_its_freezes_in_the_session_record(monkeypatch, caplog):
    source = Source(monkeypatch, [still(2000, "frozen"), moving(5, "after")])
    with caplog.at_level(logging.INFO, logger=camera_metrics.logger.name):
        await _run(source, "frozen-record")
    completed = [r.getMessage() for r in caplog.records if "Camera session completed" in r.getMessage()]
    assert len(completed) == 1 and "'frozen_restarts_total': 1" in completed[0]
