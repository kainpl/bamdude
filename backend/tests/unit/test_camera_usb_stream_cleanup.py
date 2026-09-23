"""Low-level USB capture/stream process lifecycle inside the camera worker."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from backend.app.services import external_camera


async def _instant_sleep(*_args, **_kwargs) -> None:
    """Drop-in for asyncio.sleep that returns immediately (no self-recursion)."""
    return None


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


class TestUsbSnapshotProcessOwnership:
    """Snapshot processes are tracked and released inside the worker."""

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
