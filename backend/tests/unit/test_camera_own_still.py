"""A camera with its own snapshot URL answers a one-shot PICTURE from that URL, watched or not (G09, owner's
decision 2026-11-15 on audit Unsure 1; upstream 7cea7acf0 leg 6, cf0ba31f1 leg 4).

A go2rtc-style camera has a stream URL and a still-image URL. The still is a plain HTTP GET the worker makes
beside the stream — full quality, and no second reader of the stream — so a finish photo, a timelapse frame,
a notification photo, a snapshot, a Cloud Link snapshot and a plate check take it even while somebody watches.
When the still cannot be fetched and a live view is attached, the view's frame answers; a stream reader is
never opened beside the view. Obico keeps the view's frame (it polls all print long and pairs each frame with
its arrival). The REAL ``camera_runtime.capture`` runs over a fake worker that counts what reaches it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes import camera as printer_views, cameras as camera_views
from backend.app.services import camera_fanout, camera_runtime
from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.camera_runtime import CameraCaptureRequest, capture, override_camera_runtime

LIVE = b"\xff\xd8LIVE"
STILL = b"\xff\xd8STILL"
PRINTER = 7
CAMERA = 4
STREAM_URL = "http://192.0.2.50/stream"
STILL_URL = "http://192.0.2.50/frame.jpg"


class _Worker:
    """Stands where the camera worker does. ``frame`` None is a still that could not be fetched."""

    def __init__(self) -> None:
        self.requests: list[CameraCaptureRequest] = []
        self.frame: bytes | None = STILL
        self.source: str | None = "fresh"
        self.error: Exception | None = None

    async def capture(self, request: CameraCaptureRequest) -> CameraCaptureResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return CameraCaptureResult(self.frame, self.source if self.frame else None)

    def fails(self) -> None:
        self.frame = None

    def only_stills(self) -> bool:
        """Every request that reached the worker was a fetch of the still, never a stream reader."""
        return all(request.snapshot_url == STILL_URL for request in self.requests)


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
    monkeypatch.setattr(camera_runtime, "LIVE_VIEW_FIRST_FRAME_WAIT", 0.2, raising=False)
    registries = (
        printer_views._active_worker_streams,
        printer_views._last_frames,
        printer_views._last_frame_times,
        printer_views._live_frame_metadata,
        printer_views._snapshot_frames,
        printer_views._snapshot_frame_times,
        camera_views._last_frames,
        camera_views._last_frame_times,
        camera_views._snapshot_frames,
        camera_views._snapshot_frame_times,
    )
    for registry in registries:
        registry.clear()
    camera_fanout._broadcasters.pop(f"camera-{CAMERA}", None)
    yield
    for registry in registries:
        registry.clear()
    camera_fanout._broadcasters.pop(f"camera-{CAMERA}", None)


def _watch_printer(frame: bytes | None = LIVE) -> None:
    printer_views._active_worker_streams[PRINTER] = ("token", "external")
    if frame is not None:
        printer_views._last_frames[PRINTER] = frame


def _watch_camera(frame: bytes | None = LIVE) -> None:
    camera_fanout._broadcasters[f"camera-{CAMERA}"] = SimpleNamespace(stopped=False)
    if frame is not None:
        camera_views._publish_frame(CAMERA, frame)


def _request(*, still: bool = True, **extra) -> CameraCaptureRequest:
    return CameraCaptureRequest.external(
        url=STREAM_URL, camera_type="mjpeg", snapshot_url=STILL_URL if still else None, **extra
    )


def _printer(*, still: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=PRINTER,
        name="X1",
        ip_address="10.0.0.5",
        access_code="12345678",
        model="P1S",
        camera_rotation=0,
        external_camera_enabled=True,
        external_camera_url=STREAM_URL,
        external_camera_type="mjpeg",
        external_camera_snapshot_url=STILL_URL if still else None,
    )


def _session_returning(printer):
    @asynccontextmanager
    async def fake_session():
        async def execute(_stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: printer)

        yield SimpleNamespace(execute=execute)

    return fake_session


# ---------------------------------------------------------------- the one door


@pytest.mark.asyncio
@pytest.mark.parametrize("named", ["printer", "standalone"])
class TestTheOneDoor:
    def _attach(self, named: str, frame: bytes | None = LIVE) -> CameraCaptureRequest:
        if named == "printer":
            _watch_printer(frame)
            return _request(printer_id=PRINTER)
        _watch_camera(frame)
        return _request(camera_id=CAMERA)

    async def test_a_watched_camera_answers_with_its_still(self, worker, named) -> None:
        result = await capture(self._attach(named))

        assert result.frame == STILL and result.source == "fresh"
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_a_still_that_cannot_be_fetched_falls_back_to_the_views_frame(self, worker, named) -> None:
        worker.fails()

        result = await capture(self._attach(named))

        assert result.frame == LIVE and result.source == "live_view"
        assert len(worker.requests) == 1 and worker.only_stills(), "the still is asked once, never a stream reader"

    async def test_a_worker_error_falls_back_to_the_views_frame(self, worker, named) -> None:
        worker.error = RuntimeError("worker gone")

        result = await capture(self._attach(named))

        assert result.frame == LIVE and result.source == "live_view"

    async def test_no_still_and_no_frame_yet_is_busy_never_a_stream_reader(self, worker, named) -> None:
        worker.fails()

        result = await capture(self._attach(named, None))

        assert result.frame is None and result.source == "live_view"
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_an_unwatched_camera_reports_its_own_failure(self, worker, named) -> None:
        worker.fails()
        request = _request(printer_id=PRINTER) if named == "printer" else _request(camera_id=CAMERA)

        result = await capture(request)

        assert result.frame is None and result.source is None
        assert len(worker.requests) == 1


@pytest.mark.asyncio
async def test_a_camera_without_a_still_keeps_the_live_frame(worker) -> None:
    _watch_printer()

    result = await capture(_request(still=False, printer_id=PRINTER))

    assert result.frame == LIVE and result.source == "live_view"
    assert worker.requests == []


# ---------------------------------------------------------------- notification photo and the in-print bank


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["snapshot", "finish_bank"], ids=["notification", "bank"])
class TestTheNotificationPhotoAndTheBank:
    async def _shoot(self, purpose: str, *, still: bool = True):
        from backend.app import main as main_module

        printer = _printer(still=still)
        with (
            patch.object(main_module, "async_session", _session_returning(printer)),
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        ):
            return await main_module._capture_snapshot_for_notification(
                PRINTER, printer, logging.getLogger(__name__), purpose=purpose
            )

    async def test_the_still_is_the_photo_while_watched(self, worker, purpose) -> None:
        _watch_printer()

        assert await self._shoot(purpose) == STILL
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_a_failed_still_is_the_views_frame(self, worker, purpose) -> None:
        _watch_printer()
        worker.fails()

        assert await self._shoot(purpose) == LIVE
        assert worker.only_stills()

    async def test_without_a_still_the_views_frame_is_the_photo(self, worker, purpose) -> None:
        _watch_printer()

        assert await self._shoot(purpose, still=False) == LIVE
        assert worker.requests == []


# ---------------------------------------------------------------- the finish photo at its moment


@pytest.fixture
def finish_photo(monkeypatch):
    from backend.app import main as main_module
    from backend.app.services.print_run_binding import bind_print_run
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    bind_print_run(manager, printer_id=PRINTER, archive_id=PRINTER)
    monkeypatch.setattr(main_module, "printer_manager", manager)
    caches = (
        main_module._stage22_finish_frames,
        main_module._stage22_finish_in_flight,
        main_module._inprint_frame_bank,
        main_module._inprint_frame_bank_ts,
    )
    for cache in caches:
        cache.clear()

    async def shoot(*, still: bool = True) -> bytes | None:
        monkeypatch.setattr(main_module, "async_session", _session_returning(_printer(still=still)))
        with patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")):
            await main_module.on_finish_photo_moment(PRINTER, {"trigger": "stage_22", "timelapse_was_active": False})
        return main_module._stage22_finish_frames.get(main_module._finish_photo_key(PRINTER))

    yield shoot
    for cache in caches:
        cache.clear()


@pytest.mark.asyncio
class TestTheFinishPhotoMoment:
    async def test_the_still_is_the_photo_while_watched(self, worker, finish_photo) -> None:
        _watch_printer()

        assert await finish_photo() == STILL
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_a_failed_still_is_the_views_frame(self, worker, finish_photo) -> None:
        _watch_printer()
        worker.fails()

        assert await finish_photo() == LIVE
        assert worker.only_stills()

    async def test_without_a_still_the_views_frame_is_the_photo(self, worker, finish_photo) -> None:
        _watch_printer()

        assert await finish_photo(still=False) == LIVE
        assert worker.requests == []


def test_the_post_completion_photo_asks_the_same_question() -> None:
    """``_background_finish_photo`` is a closure of ``on_print_complete`` and cannot be driven alone: pin that
    it and the finish-photo moment ask the helper that lets a camera with its own still through, and that
    nothing in main asks the bare live-view question for a picture any more."""
    from pathlib import Path

    from backend.app import main as main_module

    source = Path(main_module.__file__).read_text(encoding="utf-8")

    # The in-print bank, the finish-photo moment and the post-completion photo.
    assert source.count("live_frame_for_picture(printer_id, printer)") == 3
    assert "live_frame_for_capture" not in source


@pytest.mark.parametrize(
    ("still", "watched", "expected"),
    [(True, True, (False, None)), (False, True, (True, LIVE)), (True, False, (False, None))],
)
def test_live_frame_for_picture(still, watched, expected) -> None:
    if watched:
        _watch_printer()

    assert printer_views.live_frame_for_picture(PRINTER, _printer(still=still)) == expected


def test_a_switched_off_external_camera_has_no_still_of_its_own() -> None:
    printer = _printer()
    printer.external_camera_enabled = False
    printer_views._active_worker_streams[PRINTER] = ("token", "rtsp")
    printer_views._last_frames[PRINTER] = LIVE

    assert printer_views.live_frame_for_picture(PRINTER, printer) == (True, LIVE)


# ---------------------------------------------------------------- layer timelapse


@pytest.fixture
def timelapse(tmp_path):
    from backend.app.services import layer_timelapse

    def make(*, still: bool = True):
        with patch.object(layer_timelapse, "settings", SimpleNamespace(base_dir=tmp_path)):
            return layer_timelapse.TimelapseSession(
                PRINTER, 100, STREAM_URL, "mjpeg", snapshot_url=STILL_URL if still else None
            )

    return make


@pytest.mark.asyncio
class TestTheLayerTimelapse:
    async def test_a_watched_print_records_the_still(self, worker, timelapse) -> None:
        _watch_printer()
        session = timelapse()

        assert await session.capture_layer(1) is True
        assert (session.frames_dir / "layer_00001.jpg").read_bytes() == STILL
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_a_failed_still_records_the_views_frame(self, worker, timelapse) -> None:
        _watch_printer()
        worker.fails()
        session = timelapse()

        assert await session.capture_layer(1) is True
        assert (session.frames_dir / "layer_00001.jpg").read_bytes() == LIVE
        assert worker.only_stills()

    async def test_without_a_still_the_views_frame_is_recorded(self, worker, timelapse) -> None:
        _watch_printer()
        session = timelapse(still=False)

        assert await session.capture_layer(1) is True
        assert (session.frames_dir / "layer_00001.jpg").read_bytes() == LIVE
        assert worker.requests == []


# ---------------------------------------------------------------- Cloud Link


def _cloud_camera(*, still: bool = True):
    from backend.app.services.cloud_link import snapshot

    return snapshot._Camera(
        ip_address="10.0.0.5",
        access_code="12345678",
        model="P1S",
        external_enabled=True,
        external_url=STREAM_URL,
        external_type="mjpeg",
        external_snapshot_url=STILL_URL if still else None,
    )


@pytest.mark.asyncio
class TestCloudLink:
    async def test_a_watched_camera_sends_its_still(self, worker) -> None:
        from backend.app.services.cloud_link import snapshot

        _watch_printer()

        assert await snapshot._capture(PRINTER, _cloud_camera()) == STILL
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_a_failed_still_sends_the_views_frame(self, worker) -> None:
        from backend.app.services.cloud_link import snapshot

        _watch_printer()
        worker.fails()

        assert await snapshot._capture(PRINTER, _cloud_camera()) == LIVE
        assert worker.only_stills()

    async def test_without_a_still_the_views_frame_is_sent(self, worker) -> None:
        from backend.app.services.cloud_link import snapshot

        _watch_printer()

        assert await snapshot._capture(PRINTER, _cloud_camera(still=False)) == LIVE
        assert worker.requests == []


# ---------------------------------------------------------------- the plate check


async def _plate_image(*, still: bool = True, fresh: bool = False):
    from backend.app.services import plate_detection

    return await plate_detection.capture_camera_image(
        PRINTER,
        "10.0.0.5",
        "12345678",
        "P1S",
        external_camera_url=STREAM_URL,
        external_camera_type="mjpeg",
        use_external=True,
        external_camera_snapshot_url=STILL_URL if still else None,
        fresh=fresh,
    )


@pytest.mark.asyncio
class TestThePlateCheck:
    async def test_a_watched_camera_is_judged_on_its_still(self, worker) -> None:
        _watch_printer()

        assert await _plate_image() == (STILL, "external")
        assert len(worker.requests) == 1 and worker.only_stills()

    async def test_a_failed_still_is_judged_on_the_views_frame(self, worker) -> None:
        _watch_printer()
        worker.fails()

        assert await _plate_image() == (LIVE, "live view (buffered)")
        assert worker.only_stills()

    async def test_without_a_still_the_views_frame_is_judged(self, worker) -> None:
        _watch_printer()

        assert await _plate_image(still=False) == (LIVE, "live view (buffered)")
        assert worker.requests == []

    async def test_a_fresh_check_takes_a_still_fetched_for_it(self, worker) -> None:
        _watch_printer()

        assert await _plate_image(fresh=True) == (STILL, "external")

    async def test_a_fresh_check_refuses_somebody_elses_still(self, worker, monkeypatch) -> None:
        """A coalesced capture began before this check: not evidence of the plate now. The view's own frame
        from before the check is not either — the check waits for the view's next frame and reports none."""
        from backend.app.services import plate_detection

        monkeypatch.setattr(plate_detection, "LIVE_FRAME_WAIT_SECONDS", 0.2)
        _watch_printer()
        worker.source = "coalesced"

        image, reason = await _plate_image(fresh=True)

        assert image is None and reason == plate_detection.LIVE_VIEW_EMPTY
        assert worker.only_stills()

    async def test_the_still_is_fetched_only_after_our_light_shows(self, worker, monkeypatch) -> None:
        """The reference was calibrated in light: a still taken before the light this check switched on can
        show in a frame is not judged. ``lit_since`` may lie ahead of now (the lease's margin)."""
        import time

        lit_at = time.monotonic() + 0.15

        class _Lease:
            async def settle(self) -> None:
                return None

            def lit_since(self) -> float:
                return lit_at

        @asynccontextmanager
        async def lit(*_args, **_kwargs):
            yield _Lease()

        monkeypatch.setattr("backend.app.services.camera_light.held", lit)
        fetched: list[float] = []
        real = worker.capture

        async def stamped(request):
            fetched.append(time.monotonic())
            return await real(request)

        worker.capture = stamped
        _watch_printer()

        assert await _plate_image() == (STILL, "external")
        assert fetched and fetched[0] >= lit_at


# ---------------------------------------------------------------- Obico keeps the view's frame


@pytest.mark.asyncio
async def test_obico_keeps_the_views_frame(worker) -> None:
    """Deliberately unchanged: Obico polls every few seconds all print long and pairs each frame with the
    moment the relay received it; the view's frame costs the camera nothing."""
    from backend.app.services.obico_detection import ObicoDetectionService

    _watch_printer()
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get = AsyncMock(return_value=_printer())

    with patch("backend.app.services.obico_detection.async_session", MagicMock(return_value=db)):
        result = await ObicoDetectionService()._capture_frame(PRINTER)

    assert result.frame == LIVE and result.source == "live-buffer"
    assert worker.requests == []


# ---------------------------------------------------------------- the camera test is of the STREAM


@pytest.mark.asyncio
async def test_a_camera_test_beside_a_starting_view_is_busy_not_a_second_reader(worker) -> None:
    """The Test button checks the stream. Its request used to slip past a view that had no frame yet because
    it carried a snapshot URL — and then dialled the stream beside the view."""
    _watch_printer(None)

    answer = await camera_runtime.test_on_live_view(_request(view_printer_id=PRINTER))

    assert answer is not None and answer["success"] is False and answer["live_view"] is True
    assert worker.requests == []
