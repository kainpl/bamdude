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
def _clear_cache():
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

    assert _stage22_finish_frames.get(7) == b"\xff\xd8BUFFERED"


async def test_falls_back_to_rtsp_when_no_buffered_frame(monkeypatch):
    """No buffered frame -> fresh RTSP grab via capture_camera_frame_bytes."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=None),
        patch(
            "backend.app.services.camera.capture_camera_frame_bytes",
            new=AsyncMock(return_value=b"\xff\xd8RTSP"),
        ),
    ):
        await on_finish_photo_moment(7, {"trigger": "finish_state", "timelapse_was_active": False})

    assert _stage22_finish_frames.get(7) == b"\xff\xd8RTSP"


async def test_skips_pre_capture_when_timelapse_active(monkeypatch):
    """Timelapse recording -> skip pre-capture (extractor path handles framing)."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))
    grab = AsyncMock(return_value=b"SHOULD_NOT_RUN")

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="true")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"BUF"),
        patch("backend.app.services.camera.capture_camera_frame_bytes", new=grab),
    ):
        await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": True})

    assert 7 not in _stage22_finish_frames
    grab.assert_not_awaited()


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
        patch("backend.app.services.external_camera.capture_frame", new=AsyncMock(return_value=frame)) as external,
        patch("backend.app.services.camera.capture_camera_frame_bytes", new=AsyncMock()) as built_in,
    ):
        if consumer == "bank":
            await main_module._maybe_bank_inprint_frame(printer.id, 5)
            result = main_module._inprint_frame_bank.get(printer.id)
        else:
            result = await main_module._capture_snapshot_for_notification(
                printer.id, printer, logging.getLogger(__name__)
            )

    if setting in ("true", "TRUE"):
        assert result == frame
        external.assert_awaited_once()
    else:
        assert result is None
        external.assert_not_awaited()
    built_in.assert_not_awaited()


@pytest.mark.parametrize("setting", [None, "false", "", "1", "invalid"])
async def test_skips_when_capture_setting_not_explicitly_enabled(monkeypatch, setting):
    """Missing or non-true setting -> no pre-capture, no cache entry."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value=setting)),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"BUF"),
    ):
        await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": False})

    assert 7 not in _stage22_finish_frames
