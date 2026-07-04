"""Tests for main.on_finish_photo_moment pre-capture caching (#1721).

The handler grabs one camera frame at the stage-22 / FINISH edge and caches
the JPEG bytes in ``_stage22_finish_frames[printer_id]`` so
``_background_finish_photo`` (inside ``on_print_complete``) can consume the
better-framed pre-bed-drop frame before falling through to its own live-grab
chain. When a timelapse is actively recording the pre-capture is skipped —
the last-frame extractor gives the best framing there.
"""

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
    yield
    _stage22_finish_frames.clear()


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


async def test_skips_when_capture_setting_disabled(monkeypatch):
    """capture_finish_photo=false -> no pre-capture, no cache entry."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory(_printer()))

    with (
        patch("backend.app.api.routes.settings.get_setting", new=AsyncMock(return_value="false")),
        patch("backend.app.api.routes.camera.get_buffered_frame", return_value=b"BUF"),
    ):
        await on_finish_photo_moment(7, {"trigger": "stage_22", "timelapse_was_active": False})

    assert 7 not in _stage22_finish_frames
