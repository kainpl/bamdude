"""Tests for capture PID tracking and cleanup exclusion (#172).

The Obico detection service spawns short-lived ffmpeg processes for snapshot
capture via capture_camera_frame_bytes(). These must be registered in
_active_capture_pids so the cleanup task in routes/camera.py does not kill
them as orphaned.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.camera import (
    _active_capture_pids,
    capture_camera_frame_bytes,
)


@pytest.fixture(autouse=True)
def _clear_capture_pids(monkeypatch):
    """Ensure _active_capture_pids is empty before/after each test."""
    monkeypatch.setattr("backend.app.services.camera_tls.close_tls_proxy", AsyncMock())
    _active_capture_pids.clear()
    yield
    _active_capture_pids.clear()


class TestCapturePidRegistration:
    """Verify PIDs are added/removed from _active_capture_pids."""

    @pytest.mark.asyncio
    async def test_pid_registered_during_capture(self):
        """PID is in _active_capture_pids while ffmpeg is running."""
        observed_pids_during_run: set[int] = set()

        fake_process = MagicMock()
        fake_process.pid = 99999
        fake_process.returncode = 0

        async def fake_communicate():
            # Snapshot what's in the set while "ffmpeg is running"
            observed_pids_during_run.update(_active_capture_pids)
            return (b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9", b"")

        fake_process.communicate = fake_communicate

        fake_proxy_server = AsyncMock()
        fake_proxy_server.close = MagicMock()

        with (
            patch("backend.app.services.camera.is_chamber_image_model", return_value=False),
            patch("backend.app.services.camera.get_camera_port", return_value=322),
            patch("backend.app.services.camera.create_tls_proxy", return_value=(12345, fake_proxy_server)),
            patch("backend.app.services.camera.get_ffmpeg_path", return_value="/usr/bin/ffmpeg"),
            patch("asyncio.create_subprocess_exec", return_value=fake_process),
        ):
            result = await capture_camera_frame_bytes("192.168.1.1", "test", "P2S", timeout=10)

        # PID was registered during capture
        assert 99999 in observed_pids_during_run
        # PID is removed after capture completes
        assert 99999 not in _active_capture_pids
        # Capture returned data
        assert result is not None

    @pytest.mark.asyncio
    async def test_pid_removed_after_failure(self):
        """PID is cleaned up even when ffmpeg returns non-zero."""
        fake_process = MagicMock()
        fake_process.pid = 88888
        fake_process.returncode = 1

        async def fake_communicate():
            return (b"", b"some error")

        fake_process.communicate = fake_communicate

        fake_proxy_server = AsyncMock()
        fake_proxy_server.close = MagicMock()

        with (
            patch("backend.app.services.camera.is_chamber_image_model", return_value=False),
            patch("backend.app.services.camera.get_camera_port", return_value=322),
            patch("backend.app.services.camera.create_tls_proxy", return_value=(12345, fake_proxy_server)),
            patch("backend.app.services.camera.get_ffmpeg_path", return_value="/usr/bin/ffmpeg"),
            patch("asyncio.create_subprocess_exec", return_value=fake_process),
        ):
            result = await capture_camera_frame_bytes("192.168.1.1", "test", "P2S", timeout=10)

        assert result is None
        assert 88888 not in _active_capture_pids

    @pytest.mark.asyncio
    async def test_pid_removed_after_timeout(self):
        """PID is cleaned up when ffmpeg times out."""
        fake_process = MagicMock()
        fake_process.pid = 77777
        fake_process.returncode = None
        fake_process.kill = MagicMock()

        async def fake_communicate():
            await asyncio.sleep(60)  # Will be cancelled by wait_for
            return (b"", b"")

        fake_process.communicate = fake_communicate

        async def fake_wait():
            fake_process.returncode = -9

        fake_process.wait = fake_wait

        fake_proxy_server = AsyncMock()
        fake_proxy_server.close = MagicMock()

        with (
            patch("backend.app.services.camera.is_chamber_image_model", return_value=False),
            patch("backend.app.services.camera.get_camera_port", return_value=322),
            patch("backend.app.services.camera.create_tls_proxy", return_value=(12345, fake_proxy_server)),
            patch("backend.app.services.camera.get_ffmpeg_path", return_value="/usr/bin/ffmpeg"),
            patch("asyncio.create_subprocess_exec", return_value=fake_process),
        ):
            result = await capture_camera_frame_bytes("192.168.1.1", "test", "P2S", timeout=0.01)

        assert result is None
        assert 77777 not in _active_capture_pids

    @pytest.mark.asyncio
    async def test_no_pid_tracked_for_chamber_image_models(self):
        """Chamber image models (A1/P1) don't spawn ffmpeg — no PID tracking."""
        with (
            patch("backend.app.services.camera.is_chamber_image_model", return_value=True),
            patch("backend.app.services.camera.read_chamber_image_frame", return_value=b"\xff\xd8test\xff\xd9"),
        ):
            result = await capture_camera_frame_bytes("192.168.1.1", "test", "A1", timeout=10)

        assert result is not None
        assert len(_active_capture_pids) == 0

    @pytest.mark.asyncio
    async def test_no_pid_tracked_when_subprocess_fails(self):
        """If create_subprocess_exec raises, process is None — no PID to track."""
        fake_proxy_server = AsyncMock()
        fake_proxy_server.close = MagicMock()

        with (
            patch("backend.app.services.camera.is_chamber_image_model", return_value=False),
            patch("backend.app.services.camera.get_camera_port", return_value=322),
            patch("backend.app.services.camera.create_tls_proxy", return_value=(12345, fake_proxy_server)),
            patch("backend.app.services.camera.get_ffmpeg_path", return_value="/usr/bin/ffmpeg"),
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("ffmpeg")),
        ):
            result = await capture_camera_frame_bytes("192.168.1.1", "test", "P2S", timeout=10)

        assert result is None
        assert len(_active_capture_pids) == 0
