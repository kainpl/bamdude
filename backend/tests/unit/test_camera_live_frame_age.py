"""A watched printer's live frame has an age limit (G09 review F1).

A live stream that delivered before is redialled for its whole reconnect budget — minutes — and the relay
stays registered for all of it. Without a limit the buffer went on answering "the live view's frame" with the
picture from BEFORE the camera dropped: the finish photo, the notification photo, a Telegram snapshot,
``/camera/snapshot``, the layer timelapse, the cloud-link snapshot, Obico and the plate check all took it.
A frame whose bytes last arrived longer ago than the limit is not a frame: the answer is "watched, no frame",
which every consumer already treats as busy. Standalone cameras had this rule (``routes/cameras._live_frame``).

The buffer, its accessors and ``camera_runtime.capture`` are the real ones; the relay's writes are made by hand.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest

from backend.app.api.routes import camera as views
from backend.app.services import camera_runtime, plate_detection as pd
from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.camera_runtime import CameraCaptureRequest, capture, override_camera_runtime

LIVE = b"\xff\xd8LIVE\xff\xd9"
WORKER = b"\xff\xd8WORKER\xff\xd9"
PRINTER = 7_311


class _Worker:
    def __init__(self) -> None:
        self.requests: list[CameraCaptureRequest] = []

    async def capture(self, request: CameraCaptureRequest) -> CameraCaptureResult:
        self.requests.append(request)
        return CameraCaptureResult(WORKER, "fresh")


@pytest.fixture
def worker():
    fake = _Worker()
    with override_camera_runtime(fake):
        yield fake


@pytest.fixture(autouse=True)
def _quiet_surroundings(monkeypatch):
    @asynccontextmanager
    async def no_light(*_args, **_kwargs):
        yield None

    monkeypatch.setattr("backend.app.services.camera_light.held", no_light)
    monkeypatch.setattr("backend.app.services.camera_capability.skip_builtin_capture", lambda *_a: False)
    monkeypatch.setattr(camera_runtime, "LIVE_VIEW_FIRST_FRAME_WAIT", 0.1, raising=False)
    registries = (
        views._active_worker_streams,
        views._last_frames,
        views._last_frame_times,
        views._last_frame_arrivals,
        views._last_change_times,
        views._live_frame_metadata,
        views._snapshot_frames,
        views._snapshot_frame_times,
    )
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()


def _watch(source: str, *, bytes_arrived_ago: float) -> CameraCaptureRequest:
    """What the relay leaves behind: a producer, its frame, and when that frame's bytes last arrived."""
    request = (
        CameraCaptureRequest.external(url="http://192.0.2.50/stream", camera_type="mjpeg")
        if source == "external"
        else CameraCaptureRequest.builtin(ip_address="10.0.0.5", access_code="12345678", model="P1S")
    )
    arrived = time.monotonic() - bytes_arrived_ago
    views._active_worker_streams[PRINTER] = ("token", source)
    views._last_frames[PRINTER] = LIVE
    views._last_frame_arrivals[PRINTER] = arrived
    views._last_frame_times[PRINTER] = time.time() - bytes_arrived_ago
    views._last_change_times[PRINTER] = time.time() - bytes_arrived_ago
    views._live_frame_metadata[PRINTER] = (arrived, "token", request)
    return request


STALE = views.LIVE_FRAME_MAX_AGE_SECONDS + 1
SOURCES = ["rtsp", "chamber_image", "external"]


def _door_request(source: str) -> CameraCaptureRequest:
    if source == "external":
        return CameraCaptureRequest.external(url="http://192.0.2.50/stream", camera_type="mjpeg", printer_id=PRINTER)
    return CameraCaptureRequest.builtin(ip_address="10.0.0.5", access_code="12345678", model="P1S", printer_id=PRINTER)


@pytest.mark.parametrize("source", SOURCES)
class TestAStaleLiveFrameIsNotAFrame:
    def test_the_capture_rule_answers_watched_without_a_frame(self, source) -> None:
        """Finish photo, layer timelapse, cloud-link snapshot: ``(True, None)`` is their "busy, skip"."""
        _watch(source, bytes_arrived_ago=STALE)
        assert views.live_frame_for_capture(PRINTER) == (True, None)

    def test_the_buffer_accessors_hand_nothing_out(self, source) -> None:
        """The notification photo and ``/camera/snapshot`` read the buffer through these two."""
        _watch(source, bytes_arrived_ago=STALE)
        assert views.get_buffered_frame(PRINTER) is None
        assert views.try_get_active_buffered_frame(PRINTER) is None
        assert views._reusable_frame(PRINTER) is None

    def test_obico_is_not_given_the_frame(self, source) -> None:
        _watch(source, bytes_arrived_ago=STALE)
        defer, frame, _received, _arrived = views.live_frame_observation(PRINTER)
        assert (defer, frame) == (True, None)

    @pytest.mark.asyncio
    async def test_the_capture_door_stands_down_and_dials_nothing(self, worker, source) -> None:
        """The camera is still held by the stream that is redialling it: no fallback capture beside it."""
        _watch(source, bytes_arrived_ago=STALE)
        result = await capture(_door_request(source))
        assert result.source == "live_view" and result.frame is None
        assert worker.requests == []

    @pytest.mark.asyncio
    async def test_the_plate_check_is_told_there_is_no_frame(self, worker, source) -> None:
        _watch(source, bytes_arrived_ago=STALE)
        frame, _reason = await pd.capture_camera_image(PRINTER, "10.0.0.5", "12345678", "P1S")
        assert frame is None
        assert worker.requests == []


@pytest.mark.parametrize("source", SOURCES)
class TestAFreshLiveFrameIsLentAsBefore:
    def test_every_reader_gets_it(self, source) -> None:
        _watch(source, bytes_arrived_ago=1.0)
        assert views.live_frame_for_capture(PRINTER) == (True, LIVE)
        assert views.get_buffered_frame(PRINTER) == LIVE
        assert views.try_get_active_buffered_frame(PRINTER) == LIVE
        assert views.live_frame_observation(PRINTER)[:2] == (True, LIVE)

    @pytest.mark.asyncio
    async def test_the_capture_door_answers_from_the_view(self, worker, source) -> None:
        _watch(source, bytes_arrived_ago=1.0)
        result = await capture(_door_request(source))
        assert result.frame == LIVE and result.source == "live_view"
        assert worker.requests == []


def test_a_repeated_frame_keeps_the_buffer_alive() -> None:
    """A still scene repeats its bytes: the PICTURE is old, the bytes are not — that is a live camera."""
    _watch("rtsp", bytes_arrived_ago=1.0)
    views._live_frame_metadata[PRINTER] = (time.monotonic() - 600, "token", views._live_frame_metadata[PRINTER][2])
    assert views.live_frame_for_capture(PRINTER) == (True, LIVE)


def test_the_limit_is_of_the_standalone_cameras_order() -> None:
    from backend.app.api.routes import cameras

    assert views.LIVE_FRAME_MAX_AGE_SECONDS == cameras._LIVE_FRAME_MAX_AGE_SECONDS
