"""A one-shot capture never opens a second socket beside a live view (G09 U5).

Most firmware allows ONE camera connection, a USB camera one V4L2 handle. Until this unit the rule "take the
live view's frame, never dial beside it" was asked by each consumer on its own, and several did not ask: the
snapshot routes while a view was still starting, both camera tests, the notification photo, the in-print
frame bank on an external camera, Telegram. The rule now sits in ``camera_runtime.capture`` — the one door
every capture goes through — so these tests drive the REAL ``capture`` over a fake worker and count what
reaches it. Upstream 7cea7acf0 (legs 5, 8), cf0ba31f1 (leg 3).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.routes import camera as printer_views, cameras as camera_views
from backend.app.services import camera_fanout, camera_runtime
from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.camera_runtime import CameraCaptureRequest, capture, override_camera_runtime

LIVE = b"\xff\xd8LIVE"
WORKER = b"\xff\xd8WORKER"
PRINTER = 7
CAMERA = 4


class _Worker:
    """Stands where the camera worker does: every call here is a physical socket."""

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
    """No light, no capability lookup, a short first-frame wait, and empty registries."""

    @asynccontextmanager
    async def no_light(*_args, **_kwargs):
        yield None

    monkeypatch.setattr("backend.app.services.camera_light.held", no_light)
    monkeypatch.setattr("backend.app.services.camera_capability.skip_builtin_capture", lambda *_a: False)
    monkeypatch.setattr(camera_runtime, "LIVE_VIEW_FIRST_FRAME_WAIT", 0.3, raising=False)
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


def _watch_printer(source: str, frame: bytes | None = LIVE) -> None:
    printer_views._active_worker_streams[PRINTER] = ("token", source)
    if frame is not None:
        printer_views._last_frames[PRINTER] = frame


def _watch_camera(frame: bytes | None = LIVE) -> None:
    camera_fanout._broadcasters[f"camera-{CAMERA}"] = SimpleNamespace(stopped=False)
    if frame is not None:
        camera_views._publish_frame(CAMERA, frame)


def _builtin(**extra) -> CameraCaptureRequest:
    return CameraCaptureRequest.builtin(ip_address="10.0.0.5", access_code="12345678", model="P1S", **extra)


def _external(**extra) -> CameraCaptureRequest:
    return CameraCaptureRequest.external(url="http://192.0.2.50/stream", camera_type="mjpeg", **extra)


# What each camera kind looks like to the rule: (the view to attach, the request a consumer sends).
KINDS = {
    "rtsp": (lambda frame=LIVE: _watch_printer("rtsp", frame), lambda: _builtin(printer_id=PRINTER)),
    "chamber_image": (
        lambda frame=LIVE: _watch_printer("chamber_image", frame),
        lambda: _builtin(printer_id=PRINTER),
    ),
    "external": (lambda frame=LIVE: _watch_printer("external", frame), lambda: _external(printer_id=PRINTER)),
    "standalone": (lambda frame=LIVE: _watch_camera(frame), lambda: _external(camera_id=CAMERA)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
class TestTheOneDoor:
    async def test_a_watched_camera_answers_from_the_live_view(self, worker, kind) -> None:
        watch, request = KINDS[kind]
        watch()

        result = await capture(request())

        assert result.frame == LIVE and result.source == "live_view"
        assert worker.requests == [], "a second reader was opened beside the live view"

    async def test_an_unwatched_camera_is_captured_by_the_worker(self, worker, kind) -> None:
        _watch, request = KINDS[kind]

        result = await capture(request())

        assert result.frame == WORKER and result.source == "fresh"
        assert len(worker.requests) == 1

    async def test_a_starting_view_is_waited_for_instead_of_raced(self, worker, kind) -> None:
        watch, request = KINDS[kind]
        watch(None)  # a viewer is attached, its first frame has not landed

        async def first_frame_lands() -> None:
            await asyncio.sleep(0.05)
            watch(LIVE)

        landing = asyncio.create_task(first_frame_lands())
        result = await capture(request())
        await landing

        assert result.frame == LIVE and result.source == "live_view"
        assert worker.requests == []

    async def test_a_view_that_stays_empty_is_busy_never_a_second_socket(self, worker, kind) -> None:
        watch, request = KINDS[kind]
        watch(None)

        result = await capture(request())

        assert result.frame is None and result.source == "live_view"
        assert worker.requests == []


@pytest.mark.asyncio
class TestWhichViewCounts:
    async def test_a_view_of_the_printers_other_camera_holds_no_socket_of_ours(self, worker) -> None:
        """The view reads the EXTERNAL camera; the built-in one is another device and is free to be dialled."""
        _watch_printer("external")

        result = await capture(_builtin(printer_id=PRINTER))

        assert result.frame == WORKER and len(worker.requests) == 1

    async def test_a_caller_that_holds_the_light_itself_names_only_the_view(self, worker) -> None:
        _watch_printer("rtsp")

        result = await capture(_builtin(view_printer_id=PRINTER))

        assert result.frame == LIVE and worker.requests == []

    async def test_a_request_that_names_no_camera_is_a_plain_capture(self, worker) -> None:
        """A draft URL under test belongs to no view: nothing here may stand in for it."""
        _watch_printer("external")
        _watch_camera()

        result = await capture(_external())

        assert result.frame == WORKER and len(worker.requests) == 1

    async def test_a_snapshot_url_camera_still_takes_the_live_frame_when_there_is_one(self, worker) -> None:
        _watch_printer("external")

        result = await capture(_external(printer_id=PRINTER, snapshot_url="http://192.0.2.50/frame.jpg"))

        assert result.frame == LIVE and worker.requests == []

    async def test_a_snapshot_url_camera_is_fetched_from_its_url_while_the_view_starts(self, worker) -> None:
        """Unchanged behaviour, left for the owner (audit Unsure 1): the snapshot URL is a plain HTTP GET, not
        a second reader of the stream, so an empty view does not hold it back."""
        _watch_printer("external", None)

        result = await capture(_external(printer_id=PRINTER, snapshot_url="http://192.0.2.50/frame.jpg"))

        assert result.frame == WORKER and len(worker.requests) == 1


# ---------------------------------------------------------------- consumers in main


def _printer(*, external: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=PRINTER,
        name="X1",
        ip_address="10.0.0.5",
        access_code="12345678",
        model="P1S",
        camera_rotation=0,
        external_camera_enabled=external,
        external_camera_url="http://192.0.2.50/stream" if external else None,
        external_camera_type="mjpeg" if external else None,
        external_camera_snapshot_url=None,
    )


def _session_returning(printer):
    @asynccontextmanager
    async def fake_session():
        async def execute(_stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: printer)

        yield SimpleNamespace(execute=execute)

    return fake_session


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True], ids=["built-in", "external"])
@pytest.mark.parametrize("purpose", ["snapshot", "finish_bank"], ids=["notification", "bank"])
class TestTheNotificationPhotoAndTheBank:
    """One function serves both: the notification photo and the in-print frame bank."""

    async def _shoot(self, external: bool, purpose: str):
        from backend.app import main as main_module

        printer = _printer(external=external)
        with (
            patch.object(main_module, "async_session", _session_returning(printer)),
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        ):
            return await main_module._capture_snapshot_for_notification(
                PRINTER, printer, logging.getLogger(__name__), purpose=purpose
            )

    async def test_the_live_views_frame_is_the_photo(self, worker, external, purpose) -> None:
        _watch_printer("external" if external else "rtsp")

        assert await self._shoot(external, purpose) == LIVE
        assert worker.requests == []

    async def test_a_starting_view_costs_the_photo_not_the_viewer(self, worker, external, purpose) -> None:
        _watch_printer("external" if external else "rtsp", None)

        assert await self._shoot(external, purpose) is None
        assert worker.requests == []

    async def test_with_nobody_watching_the_worker_captures(self, worker, external, purpose) -> None:
        assert await self._shoot(external, purpose) == WORKER
        assert len(worker.requests) == 1


# ---------------------------------------------------------------- Telegram


def _telegram_surroundings(printer):
    from backend.app.services.telegram_handlers import actions, start

    return (
        patch("backend.app.core.database.async_session", _session_returning(printer)),
        patch.object(actions, "get_language", new=AsyncMock(return_value="en")),
        patch.object(actions, "has_perm", return_value=True),
        patch.object(actions, "deny_out_of_scope", new=AsyncMock(return_value=False)),
        patch.object(actions, "camera_controls", new=AsyncMock(return_value=None)),
        patch.object(start, "get_language", new=AsyncMock(return_value="en")),
        patch(
            "backend.app.services.telegram_handlers.common.get_printers_data",
            new=AsyncMock(return_value=[{"id": PRINTER, "connected": True}]),
        ),
        patch("backend.app.services.telegram_handlers.common.has_perm", return_value=True),
    )


async def _telegram_photo(door: str) -> bytes | None:
    """Run one Telegram snapshot door and return the JPEG it sent, or None when it sent the failure text."""
    from contextlib import ExitStack

    from backend.app.services.telegram_handlers import actions, start

    message = SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())
    with ExitStack() as stack:
        for item in _telegram_surroundings(_printer(external=False)):
            stack.enter_context(item)
        if door == "button":
            callback = SimpleNamespace(data=f"action:camera:{PRINTER}", answer=AsyncMock(), message=message)
            await actions.cb_camera_snapshot(callback, tg_chat=None)
        else:
            await start.cmd_camera(message, tg_chat=None)
    if not message.answer_photo.await_count:
        return None
    return message.answer_photo.await_args.kwargs["photo"].data


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["button", "command"])
class TestTelegram:
    async def test_the_live_views_frame_is_sent(self, worker, door) -> None:
        _watch_printer("chamber_image")

        assert await _telegram_photo(door) == LIVE
        assert worker.requests == []

    async def test_a_starting_view_is_not_raced(self, worker, door) -> None:
        _watch_printer("chamber_image", None)

        assert await _telegram_photo(door) is None
        assert worker.requests == []

    async def test_with_nobody_watching_the_worker_captures(self, worker, door) -> None:
        assert await _telegram_photo(door) == WORKER
        assert len(worker.requests) == 1


# ---------------------------------------------------------------- the built-in camera test


@pytest.mark.asyncio
class TestTheBuiltInCameraTest:
    async def _test(self) -> dict:
        from backend.app.services.camera import test_camera_connection

        return await test_camera_connection(
            ip_address="10.0.0.5", access_code="12345678", model="P1S", printer_id=PRINTER
        )

    async def test_a_watched_camera_passes_on_the_live_view(self, worker) -> None:
        _watch_printer("rtsp")

        answer = await self._test()

        assert answer["success"] is True and answer["live_view"] is True
        assert worker.requests == []

    async def test_a_starting_view_reads_as_busy_not_broken(self, worker) -> None:
        _watch_printer("rtsp", None)

        answer = await self._test()

        assert answer["success"] is False and answer["live_view"] is True
        assert "Developer Mode" not in answer["error"], "a busy camera must not be reported as misconfigured"
        assert worker.requests == []

    async def test_an_unwatched_camera_is_dialled(self, worker) -> None:
        answer = await self._test()

        assert answer["success"] is True and "live_view" not in answer
        assert len(worker.requests) == 1


# ---------------------------------------------------------------- consumers that already asked keep their rule


@pytest.mark.asyncio
async def test_a_fresh_plate_check_never_accepts_the_doors_live_frame(worker) -> None:
    """The automatic plate confirmation needs a frame from AFTER its start. Should a view attach between its
    own check and its capture, the door's live frame is not such a frame and must not pass for one."""
    from backend.app.services import plate_detection

    real = printer_views.live_frame_for_capture
    asked = 0

    def nobody_yet_then_a_viewer(*args, **kwargs):
        nonlocal asked
        asked += 1
        if asked == 1:  # the plate check's own question, before the view attached
            return False, None
        return real(*args, **kwargs)

    with patch("backend.app.api.routes.camera.live_frame_for_capture", nobody_yet_then_a_viewer):
        _watch_printer("rtsp")
        image, _reason = await plate_detection.capture_camera_image(PRINTER, "10.0.0.5", "12345678", "P1S", fresh=True)

    assert image is None and worker.requests == []
