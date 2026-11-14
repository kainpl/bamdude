"""Tests for main.on_finish_photo_moment pre-capture caching (#1721).

The handler grabs one camera frame at the stage-22 / FINISH edge and caches
the JPEG bytes in ``_stage22_finish_frames[printer_id]`` so
``_background_finish_photo`` (inside ``on_print_complete``) can consume the
better-framed pre-bed-drop frame before falling through to its own live-grab
chain. When a timelapse is actively recording the pre-capture is skipped —
the last-frame extractor gives the best framing there.
"""

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app import main as main_module
from backend.app.main import _stage22_finish_frames, on_finish_photo_moment
from backend.app.services.camera_metrics import CameraCaptureResult


def _fake_session_factory(printer):
    """async_session() replacement whose execute().scalar_one_or_none() returns `printer`."""

    @asynccontextmanager
    async def fake_session():
        async def execute(stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: printer)

        yield SimpleNamespace(execute=execute)

    return fake_session


def _printer():
    return SimpleNamespace(
        id=7,
        ip_address="10.0.0.5",
        access_code="12345678",
        model="P1S",
        external_camera_enabled=False,
        external_camera_url=None,
        external_camera_type=None,
        external_camera_snapshot_url=None,
    )


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    from backend.app.services.print_run_binding import bind_print_run
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    for printer_id in (3, 7):
        bind_print_run(manager, printer_id=printer_id, archive_id=printer_id)
    monkeypatch.setattr(main_module, "printer_manager", manager)
    _stage22_finish_frames.clear()
    main_module._stage22_finish_in_flight.clear()
    main_module._inprint_frame_bank.clear()
    main_module._inprint_frame_bank_ts.clear()
    yield
    _stage22_finish_frames.clear()
    main_module._stage22_finish_in_flight.clear()
    main_module._inprint_frame_bank.clear()
    main_module._inprint_frame_bank_ts.clear()


async def test_caches_buffered_frame(monkeypatch):
    """A buffered RTSP frame is cached into _stage22_finish_frames[printer_id]."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"\xff\xd8BUFFERED"),
    ):
        await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": False})

    assert _stage22_finish_frames.get(main_module._finish_photo_key(7)) == b"\xff\xd8BUFFERED"


async def test_falls_back_to_rtsp_when_no_buffered_frame(monkeypatch):
    """No buffered frame -> a fresh grab through the capture runtime."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=None),
        patch(
            "backend.app.services.camera_runtime.capture",
            new=AsyncMock(return_value=CameraCaptureResult(frame=b"\xff\xd8RTSP", source="fresh")),
        ),
    ):
        await on_finish_photo_moment(7, {"trigger": "finish_state", "timelapse_was_active": False})

    assert _stage22_finish_frames.get(main_module._finish_photo_key(7)) == b"\xff\xd8RTSP"


async def test_skips_pre_capture_when_timelapse_active(monkeypatch):
    """Timelapse recording -> skip pre-capture (extractor path handles framing)."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))
    grab = AsyncMock(return_value=b"SHOULD_NOT_RUN")

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"BUF"),
        patch("backend.app.services.camera_runtime.capture", new=grab),
    ):
        await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": True})

    assert main_module._finish_photo_key(7) not in _stage22_finish_frames
    grab.assert_not_awaited()


async def test_foreign_finish_edge_does_not_capture_for_the_active_run(monkeypatch):
    """A late terminal edge from A must not bank a photo that B will consume."""
    conflict = AsyncMock(return_value=True)
    monkeypatch.setattr(main_module, "_completion_conflicts_with_active_queue", conflict)
    capture = AsyncMock()
    monkeypatch.setattr("backend.app.services.camera_runtime.capture", capture)

    await on_finish_photo_moment(
        7,
        {"trigger": "finish_state", "subtask_name": "Old A", "subtask_id": "101"},
    )

    conflict.assert_awaited_once()
    capture.assert_not_awaited()
    assert main_module._finish_photo_key(7) not in _stage22_finish_frames


@pytest.mark.parametrize("setting, enabled", [(None, False), ("false", False), ("true", True)])
async def test_settings_response_requires_saved_opt_in(db_session, setting, enabled):
    from backend.app.api.routes.settings import get_settings, set_setting

    if setting is not None:
        await set_setting(db_session, "capture_finish_photo", setting)
    result = await get_settings(db=db_session, _=None)
    assert result.capture_finish_photo is enabled


@pytest.mark.parametrize("setting", [None, "false", "", "1", "invalid", "true", "TRUE"])
@pytest.mark.parametrize("consumer", ["notification", "bank"])
async def test_snapshot_consumers_require_explicit_opt_in(monkeypatch, setting, consumer):
    """Exercise the real shared gate: no camera IO or banked frame without opt-in."""
    printer = _printer()
    printer.external_camera_enabled = True
    printer.external_camera_url = "http://camera.example/snapshot"
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(printer))
    monkeypatch.setattr(
        main_module.printer_manager,
        "get_client",
        lambda _pid: SimpleNamespace(
            state=SimpleNamespace(state="RUNNING", mc_print_sub_stage=0, total_layers=10),
            _finish_photo_captured=False,
        ),
    )
    frame = b"\xff\xd8FRAME"
    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value=setting)),
        patch(
            "backend.app.services.camera_runtime.capture",
            new=AsyncMock(return_value=CameraCaptureResult(frame=frame, source="fresh")),
        ) as capture,
    ):
        if consumer == "bank":
            await main_module._maybe_bank_inprint_frame(printer.id, 5)
            result = main_module._inprint_frame_bank.get(main_module._finish_photo_key(printer.id))
        else:
            result = await main_module._capture_snapshot_for_notification(
                printer.id, printer, logging.getLogger(__name__)
            )

    if setting in ("true", "TRUE"):
        assert result == frame
        capture.assert_awaited_once()
    else:
        assert result is None
        capture.assert_not_awaited()


def _running_client(monkeypatch):
    monkeypatch.setattr(
        main_module.printer_manager,
        "get_client",
        lambda _pid: SimpleNamespace(
            state=SimpleNamespace(state="RUNNING", mc_print_sub_stage=0, total_layers=0),
            _finish_photo_captured=False,
        ),
    )


@pytest.mark.parametrize("external", [False, True])
async def test_the_bank_captures_under_its_own_purpose(monkeypatch, external):
    """Both capture paths carry ``finish_bank``; a notification photo keeps ``snapshot``."""
    printer = _printer()
    if external:
        printer.external_camera_enabled = True
        printer.external_camera_url = "http://camera.example/snapshot"
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(printer))
    _running_client(monkeypatch)
    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.is_stream_active", return_value=False),
        patch(
            "backend.app.services.camera_runtime.capture",
            new=AsyncMock(return_value=CameraCaptureResult(frame=b"\xff\xd8F", source="fresh")),
        ) as capture,
    ):
        await main_module._maybe_bank_inprint_frame(printer.id, 5)
        assert capture.await_args.args[0].purpose == "finish_bank"
        await main_module._capture_snapshot_for_notification(printer.id, printer, logging.getLogger(__name__))
        assert capture.await_args.args[0].purpose == "snapshot"


async def test_the_bank_stands_down_for_a_starting_view_of_an_external_camera(monkeypatch):
    """A USB camera has one handle: a view with no frame yet stops the bank on an external camera too (G09 U5)."""
    printer = _printer()
    printer.external_camera_enabled = True
    printer.external_camera_url = "/dev/video0"
    printer.external_camera_type = "usb"
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(printer))
    _running_client(monkeypatch)
    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.live_frame_for_capture", return_value=(True, None)),
        patch(
            "backend.app.services.camera_runtime.capture",
            new=AsyncMock(return_value=CameraCaptureResult(frame=b"\xff\xd8F", source="fresh")),
        ) as capture,
    ):
        await main_module._maybe_bank_inprint_frame(printer.id, 5)
    capture.assert_not_awaited()
    assert main_module._finish_photo_key(printer.id) not in main_module._inprint_frame_bank


async def test_banking_through_the_real_capture_never_switches_the_light(monkeypatch):
    """Layer after layer through ``camera_runtime.capture`` and the real lease: no ledctrl at all."""
    from backend.app.services import camera_light, camera_runtime

    printer = _printer()
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(printer))
    _running_client(monkeypatch)
    monkeypatch.setattr(main_module, "_INPRINT_BANK_MIN_INTERVAL", 0.0)

    commands: list[bool] = []
    light_client = SimpleNamespace(
        state=SimpleNamespace(connected=True, chamber_light=False, has_chamber_light=True),
        set_chamber_light=lambda on: commands.append(on) or True,
    )

    async def farm_light_on(_printer_id, purpose):
        # The farm toggle is on for this printer; only the purpose can say no.
        return purpose not in camera_light.NEVER_PURPOSES

    monkeypatch.setattr(camera_light, "_get_client", lambda _pid: light_client)
    monkeypatch.setattr(camera_light, "allowed", farm_light_on)
    camera_light._lights.clear()
    runtime = SimpleNamespace(capture=AsyncMock(return_value=CameraCaptureResult(frame=b"\xff\xd8F", source="fresh")))
    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: runtime)
    try:
        with (
            patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
            patch("backend.app.api.routes.camera.is_stream_active", return_value=False),
        ):
            for layer in (5, 6, 7, 8):
                await main_module._maybe_bank_inprint_frame(printer.id, layer)
        assert runtime.capture.await_count == 4
        assert {call.args[0].purpose for call in runtime.capture.await_args_list} == {"finish_bank"}
        assert commands == []
        assert printer.id not in camera_light._lights
    finally:
        camera_light._lights.clear()


async def test_a_finish_from_the_bank_neither_captures_nor_takes_the_light(monkeypatch):
    """The FINISH fallback that uses banked bytes must not light or re-shoot the (swapped) plate."""
    from backend.app.services import camera_light

    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))
    main_module._inprint_frame_bank[main_module._finish_photo_key(7)] = b"\xff\xd8BANKED"
    acquire = AsyncMock()
    monkeypatch.setattr(camera_light, "acquire", acquire)
    capture = AsyncMock()
    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=None),
        patch("backend.app.services.camera_runtime.capture", new=capture),
    ):
        await on_finish_photo_moment(7, {"trigger": "finish_state", "timelapse_was_active": False})

    capture.assert_not_awaited()
    acquire.assert_not_awaited()
    assert _stage22_finish_frames.get(main_module._finish_photo_key(7)) == b"\xff\xd8BANKED"


@pytest.mark.parametrize("setting", [None, "false", "", "1", "invalid"])
async def test_skips_when_capture_setting_not_explicitly_enabled(monkeypatch, setting):
    """Missing or non-true setting -> no pre-capture, no cache entry."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value=setting)),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"BUF"),
    ):
        await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": False})

    assert main_module._finish_photo_key(7) not in _stage22_finish_frames
