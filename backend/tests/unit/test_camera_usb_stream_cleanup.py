"""External/USB camera ffmpeg-leak cleanup (upstream #2675).

An external USB (V4L2) camera's ffmpeg used to be reachable only from its own
stream generator's ``finally`` — which an abrupt client disconnect can skip
(the same cancellation-timing class as #776). Because external streams never
registered into ``_active_streams`` / ``_disconnect_events`` / the spawned-PID
map, both ``/camera/stop`` and ``cleanup_orphaned_streams`` were structurally
blind to the leak, and ``/dev/videoN`` stayed locked with the LED on.

The last class covers something upstream's fix does *not*: widening the /proc
sweep to match ``-f v4l2`` also puts our short-lived USB **snapshot** captures
under the same net, and those had no exemption.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from unittest.mock import mock_open, patch

import pytest

from backend.app.api.routes import camera
from backend.app.services import external_camera


async def _instant_sleep(*_args, **_kwargs) -> None:
    """Drop-in for asyncio.sleep that returns immediately (no self-recursion)."""
    return None


class _CleanProc:
    """ffmpeg that terminates cleanly when asked."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class _ImmediateEOFReader:
    async def read(self, _size: int = -1) -> bytes:
        return b""


class _UsbProc:
    """ffmpeg for a USB stream: yields no frames, exits at first read."""

    def __init__(self, pid: int = 52001) -> None:
        self.pid = pid
        self.returncode = None
        self.stdout = _ImmediateEOFReader()
        self.stderr = _ImmediateEOFReader()

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return 0


class _FakePath:
    def __init__(self, _p: str) -> None:
        pass

    def exists(self) -> bool:
        return True


class TestStreamHandsOverItsProcess:
    """The linchpin: the generator must expose its ffmpeg to the route layer."""

    @pytest.mark.asyncio
    async def test_stream_usb_registers_process_via_on_process(self, monkeypatch):
        proc = _UsbProc()

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return proc

        monkeypatch.setattr(external_camera, "get_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(external_camera, "Path", _FakePath)
        monkeypatch.setattr(external_camera.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(external_camera.asyncio, "sleep", _instant_sleep)

        captured: list[object] = []
        stream = external_camera._stream_usb("/dev/video0", 10, on_process=captured.append)
        try:
            async for _frame in stream:
                pass
        finally:
            with suppress(Exception):
                await stream.aclose()

        assert captured == [proc], "the spawned ffmpeg must be handed to on_process"

    @pytest.mark.asyncio
    async def test_registration_happens_before_the_startup_probe(self, monkeypatch):
        """A process that HANGS on a locked device never reaches the probe. If we
        registered after it, the one process most worth reaping would be the one
        we could not see."""
        proc = _UsbProc()
        order: list[str] = []

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return proc

        async def probing_sleep(*_args, **_kwargs):
            order.append("probe")

        monkeypatch.setattr(external_camera, "get_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(external_camera, "Path", _FakePath)
        monkeypatch.setattr(external_camera.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(external_camera.asyncio, "sleep", probing_sleep)

        stream = external_camera._stream_usb("/dev/video0", 10, on_process=lambda _p: order.append("register"))
        try:
            async for _frame in stream:
                pass
        finally:
            with suppress(Exception):
                await stream.aclose()

        assert order[:2] == ["register", "probe"]


class TestStopEndpointReachesExternalStreams:
    """The reported symptom was `/camera/stop` answering {"stopped": 0} for a
    live USB stream. It now finds one."""

    @pytest.mark.asyncio
    async def test_stop_endpoint_terminates_registered_external_process(self, monkeypatch):
        monkeypatch.setattr(camera, "_FFMPEG_KILL_TIMEOUT", 0.05)
        monkeypatch.setattr(camera, "get_subscriber_count", lambda _key: 0)

        async def fake_shutdown(_key):
            return False

        monkeypatch.setattr(camera, "shutdown_broadcaster", fake_shutdown)

        printer_id = 7
        sid = f"{printer_id}-ext-abc12345"
        proc = _CleanProc(pid=52010)
        event = asyncio.Event()
        camera._active_streams[sid] = proc
        camera._disconnect_events[sid] = event
        camera._spawned_ffmpeg_pids[proc.pid] = time.time()
        camera._stream_last_frame_times[sid] = time.time()

        try:
            result = await camera.stop_camera_stream(printer_id, _=None)
            assert result["stopped"] == 1
            assert proc.returncode is not None, "the external ffmpeg must be terminated"
            assert event.is_set(), "the stop event must be signalled so the loop does not respawn"
            # Registry fully cleaned, so it cannot be double-reaped.
            assert sid not in camera._active_streams
            assert sid not in camera._disconnect_events
            assert proc.pid not in camera._spawned_ffmpeg_pids
        finally:
            camera._active_streams.pop(sid, None)
            camera._disconnect_events.pop(sid, None)
            camera._spawned_ffmpeg_pids.pop(proc.pid, None)
            camera._stream_last_frame_times.pop(sid, None)


class TestJanitorReachesExternalStreams:
    @pytest.mark.asyncio
    async def test_cleanup_janitor_reaps_stale_external_usb_stream(self, monkeypatch):
        monkeypatch.setattr(camera, "_FFMPEG_KILL_TIMEOUT", 0.05)
        monkeypatch.setattr(camera, "_scan_bambu_ffmpeg_pids", lambda: [])

        import os

        proc = _CleanProc(pid=os.getpid())  # a real pid, so the existence check keeps it
        sid = "7-ext-deadbeef"
        now = time.time()
        camera._active_streams[sid] = proc
        camera._spawned_ffmpeg_pids[proc.pid] = now - 120  # spawned long ago
        camera._stream_last_frame_times[sid] = now - 60  # stale: no frames for >30s
        camera._disconnect_events[sid] = asyncio.Event()

        try:
            await asyncio.wait_for(camera.cleanup_orphaned_streams(), timeout=2.0)
            assert proc.returncode is not None, "a stale external ffmpeg must be killed"
            assert sid not in camera._active_streams
        finally:
            camera._active_streams.pop(sid, None)
            camera._spawned_ffmpeg_pids.pop(proc.pid, None)
            camera._stream_last_frame_times.pop(sid, None)
            camera._disconnect_events.pop(sid, None)


class TestProcSafetyNet:
    """The /proc sweep is the layer that survives an app restart."""

    def test_scan_matches_v4l2_ffmpeg(self, monkeypatch):
        cmdline = b"ffmpeg\x00-f\x00v4l2\x00-i\x00/dev/video0\x00-f\x00mjpeg\x00-\x00"
        monkeypatch.setattr("os.listdir", lambda _p: ["52020"])
        with patch("builtins.open", mock_open(read_data=cmdline)):
            assert 52020 in camera._scan_bambu_ffmpeg_pids()

    def test_scan_still_matches_bambu_rtsp(self, monkeypatch):
        cmdline = b"ffmpeg\x00-i\x00rtsps://bblp:code@192.168.1.5:322/streaming/live/1\x00-\x00"
        monkeypatch.setattr("os.listdir", lambda _p: ["52022"])
        with patch("builtins.open", mock_open(read_data=cmdline)):
            assert 52022 in camera._scan_bambu_ffmpeg_pids()

    def test_scan_ignores_unrelated_ffmpeg(self, monkeypatch):
        # A transcode of a local file is not ours — it must not be reaped.
        cmdline = b"ffmpeg\x00-i\x00/home/user/movie.mp4\x00out.mkv\x00"
        monkeypatch.setattr("os.listdir", lambda _p: ["52021"])
        with patch("builtins.open", mock_open(read_data=cmdline)):
            assert camera._scan_bambu_ffmpeg_pids() == []

    def test_scan_ignores_v4l2_ctl_which_is_not_ffmpeg(self, monkeypatch):
        # Device enumeration shells out to v4l2-ctl. It carries "v4l2" but is a
        # different binary, and the ffmpeg precondition is what keeps it out.
        cmdline = b"v4l2-ctl\x00-d\x00/dev/video0\x00--info\x00"
        monkeypatch.setattr("os.listdir", lambda _p: ["52023"])
        with patch("builtins.open", mock_open(read_data=cmdline)):
            assert camera._scan_bambu_ffmpeg_pids() == []


class TestUsbSnapshotIsExemptFromTheWiderNet:
    """Ours, beyond upstream.

    Widening the sweep to ``-f v4l2`` also matches our USB *snapshot* command —
    finish photos and timelapse layer frames. Those are short-lived and not in
    ``_active_streams``, so a cleanup tick landing mid-capture would SIGKILL one
    and store the truncated JPEG. That is #979 all over again; the exemption
    registry that fixed it for the Bambu path now covers this one too.
    """

    @pytest.mark.asyncio
    async def test_capture_registers_and_releases_its_pid(self, monkeypatch):
        from backend.app.services.camera import _active_capture_pids

        seen_during_capture: list[bool] = []

        class _CaptureProc:
            pid = 52030
            returncode = 0

            async def communicate(self):
                # The window the janitor could fire in.
                seen_during_capture.append(self.pid in _active_capture_pids)
                return b"\xff\xd8" + b"x" * 200 + b"\xff\xd9", b""

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return _CaptureProc()

        monkeypatch.setattr(external_camera, "get_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(external_camera, "Path", _FakePath)
        monkeypatch.setattr(external_camera.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        result = await external_camera._capture_usb_frame("/dev/video0", timeout=5)

        assert result is not None
        assert seen_during_capture == [True], "the capture pid must be exempt while it runs"
        assert 52030 not in _active_capture_pids, "and released afterwards"

    @pytest.mark.asyncio
    async def test_pid_is_released_even_when_the_capture_fails(self, monkeypatch):
        from backend.app.services.camera import _active_capture_pids

        class _FailingProc:
            pid = 52031
            returncode = 1

            async def communicate(self):
                raise TimeoutError

            def kill(self):
                pass

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return _FailingProc()

        monkeypatch.setattr(external_camera, "get_ffmpeg_path", lambda: "/fake/ffmpeg")
        monkeypatch.setattr(external_camera, "Path", _FakePath)
        monkeypatch.setattr(external_camera.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        assert await external_camera._capture_usb_frame("/dev/video0", timeout=5) is None
        # A leaked exemption would make that pid permanently unreapable — the
        # janitor's blind spot, inverted.
        assert 52031 not in _active_capture_pids
