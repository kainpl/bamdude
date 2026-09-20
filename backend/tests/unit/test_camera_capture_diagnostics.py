"""Camera failures on a busy farm must be attributable without logging secrets."""

import asyncio
import logging
import re
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.services import camera
from backend.app.utils.ffmpeg_output import NO_FFMPEG_OUTPUT, summarize_ffmpeg_stderr

_BANNER = (
    "ffmpeg version 7.1.4 Copyright FFmpeg developers\n"
    "  built with gcc 14\n"
    "  configuration: " + "--enable-feature " * 100 + "\n"
    "  libavcodec     61.19 / 61.19\n"
)
_FRAME = b"\xff\xd8" + b"x" * 200 + b"\xff\xd9"


@pytest.mark.parametrize("data", [None, "", b"", _BANNER])
def test_empty_or_banner_only_has_no_diagnostic(data):
    assert summarize_ffmpeg_stderr(data) == ""


def test_binary_diagnosis_survives_banner_and_credentials_are_masked():
    stderr = _BANNER.encode() + (
        b"[rtsp] Invalid data \xff found when processing input\n"
        b"Error opening input rtsp://bblp:private-code@127.0.0.1:43210/streaming/live/1\n"
    )
    summary = summarize_ffmpeg_stderr(stderr)
    assert "Invalid data" in summary
    assert "127.0.0.1:43210" in summary
    assert "[REDACTED]" in summary
    assert "private-code" not in summary
    assert "configuration:" not in summary
    assert "ffmpeg version" not in summary


def test_password_straddling_character_cut_is_redacted_before_truncation():
    secret = "secret-" * 1000
    stderr = "x" * 3000 + f" rtsp://user:{secret}@192.0.2.1/live Connection refused"
    summary = summarize_ffmpeg_stderr(stderr)
    assert len(summary) <= 2003
    assert "secret-" not in summary
    assert "[REDACTED]" in summary
    assert summary.endswith("Connection refused")


@pytest.fixture
def capture_process(monkeypatch, caplog):
    """No camera connections or subprocesses, including on an unexpected path."""
    monkeypatch.setattr(camera, "_inflight_captures", {})
    monkeypatch.setattr(camera, "_active_capture_pids", set())
    proxy = Mock()
    proxy.wait_closed = AsyncMock()
    monkeypatch.setattr(camera, "create_tls_proxy", AsyncMock(return_value=(43210, proxy)))

    async def close_proxy(server):
        server.close()
        await server.wait_closed()

    monkeypatch.setattr("backend.app.services.camera_tls.close_tls_proxy", close_proxy)
    monkeypatch.setattr(camera, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(camera, "read_chamber_image_frame", AsyncMock(return_value=_FRAME))
    process = Mock(pid=4321, returncode=3199971767)
    process.communicate = AsyncMock(return_value=(b"", b"Invalid data found when processing input"))
    process.wait = AsyncMock()
    monkeypatch.setattr(camera.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    caplog.set_level(logging.DEBUG, logger=camera.__name__)
    return process


def _messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == camera.__name__]


def _capture_id(message):
    match = re.search(r"capture_id=(camera-capture-[0-9a-f]{12})", message)
    assert match is not None, message
    return match[1]


async def test_failure_keeps_target_attempt_duration_exit_and_real_diagnosis(capture_process, caplog):
    capture_process.communicate.return_value = (
        b"",
        _BANNER.encode() + b"Error opening rtsp://bblp:private-code@127.0.0.1:43210/live: Invalid data \xff\n",
    )
    result = await camera.capture_camera_frame_bytes("192.0.2.5", "private-code", "X1C")
    assert result is None
    start, failure = _messages(caplog)
    assert _capture_id(start) == _capture_id(failure)
    assert "target=192.0.2.5:322 model=X1C protocol=rtsp" in failure
    assert "code 3199971767" in failure
    assert "pid=4321" in failure
    assert re.search(r"elapsed=\d+\.\d{3}s", failure)
    assert "Invalid data" in failure
    assert "private-code" not in caplog.text
    assert "configuration:" not in caplog.text
    assert camera._active_capture_pids == set()


async def test_two_printers_have_distinct_attempt_ids(capture_process, caplog):
    await asyncio.gather(
        camera.capture_camera_frame_bytes("192.0.2.5", "code", "X1C"),
        camera.capture_camera_frame_bytes("192.0.2.6", "code", "H2D"),
    )
    failures = [msg for msg in _messages(caplog) if "capture failed" in msg]
    assert len(failures) == 2
    assert len({_capture_id(msg) for msg in failures}) == 2
    for target in ("192.0.2.5", "192.0.2.6"):
        attempt = [msg for msg in _messages(caplog) if f"target={target}:" in msg]
        assert len(attempt) == 2
        assert _capture_id(attempt[0]) == _capture_id(attempt[1])


async def test_follower_timeout_names_capture_that_keeps_running(capture_process, caplog):
    started, finish = asyncio.Event(), asyncio.Event()

    async def communicate():
        started.set()
        await finish.wait()
        return _FRAME, b""

    capture_process.returncode = 0
    capture_process.communicate.side_effect = communicate
    leader = asyncio.create_task(camera.capture_camera_frame_bytes("192.0.2.5", "code", "X1C"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await camera.capture_camera_frame_bytes("192.0.2.5", "code", "X1C", timeout=0) is None
        assert not leader.done()
    finally:
        finish.set()
        result = await leader
    assert result == _FRAME
    assert capture_process.communicate.await_count == 1
    capture_process.kill.assert_not_called()
    messages = _messages(caplog)
    assert len({_capture_id(msg) for msg in messages}) == 1
    assert any("Gave up waiting" in msg and "elapsed=" in msg for msg in messages)
    assert any("Successfully captured" in msg for msg in messages)


async def test_process_timeout_logs_context_and_still_reaps_process(capture_process, caplog):
    capture_process.communicate.side_effect = TimeoutError
    capture_process.returncode = None

    async def reaped():
        capture_process.returncode = -15

    capture_process.wait.side_effect = reaped
    assert await camera.capture_camera_frame_bytes("192.0.2.5", "code", "X1C") is None
    failure = next(msg for msg in _messages(caplog) if "timed out" in msg)
    assert "target=192.0.2.5:322" in failure
    assert "pid=4321" in failure and "elapsed=" in failure
    _capture_id(failure)
    capture_process.terminate.assert_called_once()
    capture_process.wait.assert_awaited_once()
    assert camera._active_capture_pids == set()


async def test_exception_trace_does_not_reintroduce_url_secret(capture_process, caplog):
    capture_process.communicate.side_effect = RuntimeError("failed rtsp://bblp:private-code@127.0.0.1:43210/live")
    assert await camera.capture_camera_frame_bytes("192.0.2.5", "private-code", "X1C") is None
    assert "exception=RuntimeError" in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "private-code" not in caplog.text
    assert "target=192.0.2.5:322" in caplog.text


@pytest.mark.parametrize("stdout", [b"", b"short-frame"])
async def test_zero_exit_with_no_usable_frame_is_explained(capture_process, caplog, stdout):
    capture_process.returncode = 0
    capture_process.communicate.return_value = stdout, _BANNER.encode()
    assert await camera.capture_camera_frame_bytes("192.0.2.5", "code", "X1C") is None
    assert "failed (code 0)" in caplog.text
    assert f"bytes={len(stdout)}" in caplog.text
    assert NO_FFMPEG_OUTPUT in caplog.text


async def test_chamber_capture_uses_same_diagnostic_context(capture_process, caplog):
    assert await camera.capture_camera_frame_bytes("192.0.2.5", "code", "P1S") == _FRAME
    start, success = _messages(caplog)
    assert _capture_id(start) == _capture_id(success)
    assert "target=192.0.2.5:6000 model=P1S protocol=chamber" in success
    assert "elapsed=" in success and "succeeded" in success
    camera.create_tls_proxy.assert_not_awaited()
