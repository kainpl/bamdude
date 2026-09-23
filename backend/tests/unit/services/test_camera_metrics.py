"""Producer evidence must not turn shared work into fake connects or retain data."""

import asyncio
import json
from functools import partial
from unittest.mock import AsyncMock

import pytest

from backend.app.api.routes import camera as routes
from backend.app.services import camera, camera_metrics as metrics, external_camera
from backend.app.services.camera_cleanup import CameraAttempt
from backend.app.services.camera_fanout import MjpegBroadcaster

JPEG = b"\xff\xd8" + b"synthetic" * 20 + b"\xff\xd9"
PART = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"


@pytest.fixture(autouse=True)
def isolated_evidence(monkeypatch):
    monkeypatch.setattr(metrics, "_active", {})
    monkeypatch.setattr(metrics, "_completed", metrics.OrderedDict())
    monkeypatch.setattr(metrics, "_deliveries", metrics.OrderedDict())
    monkeypatch.setattr(camera, "_inflight_captures", {})
    monkeypatch.setattr(external_camera, "_inflight_captures", {})


@pytest.mark.asyncio
async def test_stream_context_does_not_escape_yield_and_close_finishes_once(caplog):
    closed = []

    @metrics.observed_stream("rtsp")
    async def source(printer_id=42, stream_id="live-42"):
        metrics.current.get().begin_attempt()
        try:
            yield metrics.record_frame(JPEG)
        finally:
            closed.append(metrics.current.get().session_id)

    with caplog.at_level("INFO", logger=metrics.__name__):
        stream = source()
        assert await anext(stream) == JPEG
        assert metrics.current.get() is None
        assert metrics.for_printer(42)["active"] is True
        await stream.aclose()
        await stream.aclose()
    assert closed == ["live-42"]
    assert not metrics._active
    evidence = metrics.for_printer(42)
    assert evidence["end_reason"] == "client_disconnected"
    assert evidence["output_frames"] == 1
    assert sum("Camera session completed" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_shared_capture_keeps_producer_id_and_first_frame_time(monkeypatch, external):
    opened, release = asyncio.Event(), asyncio.Event()

    async def capture(*args):
        opened.set()
        await release.wait()
        metrics.record_frame(JPEG)
        return JPEG

    if external:
        monkeypatch.setattr(external_camera, "_capture_frame_uncoalesced", capture)
        call = partial(external_camera.capture_frame_with_provenance, "http://user:secret@camera", "snapshot")
    else:
        monkeypatch.setattr(camera, "_capture_camera_frame_bytes_uncoalesced", capture)
        call = partial(camera.capture_camera_frame_with_provenance, "192.0.2.1", "secret", "P1S")
    leader = asyncio.create_task(call())
    await opened.wait()
    follower = asyncio.create_task(call())
    await asyncio.sleep(0)
    release.set()
    own, shared = await asyncio.gather(leader, follower)
    assert own.source == "fresh"
    assert shared.source == "coalesced"
    assert own.attempt_id == shared.attempt_id
    assert own.first_frame_ms == shared.first_frame_ms
    assert own.first_frame_ms is not None
    assert shared.caller_wait_ms is not None
    assert not metrics._active
    serialized = json.dumps([m.snapshot() for m in metrics._completed.values()])
    assert "secret" not in serialized and "192.0.2.1" not in serialized


@pytest.mark.asyncio
async def test_failed_leader_followed_by_own_capture_is_not_shared(monkeypatch):
    opened, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def capture(*args):
        calls.append(metrics.current.get().attempt_id)
        if len(calls) == 1:
            opened.set()
            await release.wait()
            return None
        return metrics.record_frame(JPEG)

    monkeypatch.setattr(camera, "_capture_camera_frame_bytes_uncoalesced", capture)
    leader = asyncio.create_task(camera.capture_camera_frame_with_provenance("192.0.2.1", "secret", "P1S"))
    await opened.wait()
    follower = asyncio.create_task(camera.capture_camera_frame_with_provenance("192.0.2.1", "secret", "P1S"))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(leader, follower)
    assert first.frame is None
    assert second.source == "fresh"
    assert second.attempt_id == calls[1] != calls[0]
    # One last-completed record for the same source, not one per capture.
    assert len(metrics._completed) == 1


@pytest.mark.asyncio
async def test_frame_clock_precedes_cleanup_and_caller_wait(monkeypatch):
    original_close = CameraAttempt._close

    async def delayed_close(self):
        await asyncio.sleep(0.02)
        await original_close(self)

    async def capture(*args):
        async with CameraAttempt("synthetic"):
            return metrics.record_frame(JPEG)

    monkeypatch.setattr(CameraAttempt, "_close", delayed_close)
    monkeypatch.setattr(camera, "_capture_camera_frame_bytes_uncoalesced", capture)
    result = await camera.capture_camera_frame_with_provenance("192.0.2.1", "secret", "X1C")
    assert result.first_frame_ms is not None
    assert result.cleanup_ms is not None
    assert result.caller_wait_ms > result.first_frame_ms + 10


@pytest.mark.asyncio
async def test_slow_viewer_counts_only_dropped_jpeg_parts():
    pumped, release = asyncio.Event(), asyncio.Event()

    @metrics.observed_stream("chamber_image")
    async def source(disconnect_event, printer_id=42, stream_id="slow-viewer"):
        metrics.current.get().begin_attempt()
        for _ in range(12):
            metrics.record_frame(JPEG)
            yield PART
        yield b"--frame\r\nContent-Type: text/plain\r\n\r\nerror"
        pumped.set()
        await release.wait()

    broadcaster = MjpegBroadcaster("printer-42", source)
    await broadcaster.subscribe()
    await pumped.wait()
    evidence = metrics.for_printer(42)
    assert evidence["output_frames"] == 12
    assert evidence["subscriber_dropped_frames"] == 8
    await broadcaster.force_shutdown()
    assert not metrics._active


def test_completed_records_and_delivery_are_bounded_and_forget_clears_identity(monkeypatch):
    monkeypatch.setattr(metrics, "_MAX_COMPLETED", 4)
    for printer_id in range(20):
        record = metrics.CameraMetrics("rtsp", printer_id=printer_id)
        record.begin_attempt()
        record.finish("upstream_ended")
        metrics.remember_delivery(printer_id, "snapshot_cache")
    assert len(metrics._completed) == len(metrics._deliveries) == 4
    assert metrics.for_printer(0) is None
    assert metrics.for_printer(19) is not None
    live = metrics.CameraMetrics("rtsp", printer_id=19)
    metrics.forget_printer(19)
    live.finish("client_disconnected")
    assert metrics.for_printer(19) is None
    assert metrics.delivery_for_printer(19) is None
    assert not any(isinstance(v, (bytes, asyncio.Task)) for v in vars(live).values())


def test_retry_reset_polling_and_source_evidence_are_not_fabricated():
    record = metrics.CameraMetrics("rtsp")
    record.begin_attempt()
    record.frame(b"not a jpeg")
    assert record.first_frame_ms is None and record.output_frames == 0
    record.frame(JPEG)
    record.begin_attempt()
    assert record.first_frame_ms is None
    assert record.snapshot()["reconnects_total"] == 1
    record.begin_attempt(reconnect=False)
    assert record.snapshot()["reconnects_total"] == 1
    record.observe_stderr("Input rtsp://user:secret@host\nStream: Video: h264 (High), yuv420p, 1920x1080")
    assert record.snapshot()["source_codec"] == "h264"
    assert record.snapshot()["source_resolution"] == [1920, 1080]
    assert "secret" not in json.dumps(record.snapshot())
    record.finish("upstream_ended")


@pytest.mark.asyncio
async def test_status_is_read_only_and_cache_has_no_fake_connect_time(monkeypatch):
    monkeypatch.setattr(routes, "_active_worker_streams", {})
    no_capture = AsyncMock(side_effect=AssertionError("status must never open a camera"))
    monkeypatch.setattr(camera, "capture_camera_frame_with_provenance", no_capture)
    metrics.remember_delivery(42, "snapshot_cache")
    result = await routes.camera_status(42, None)
    assert result["telemetry"] is None
    assert result["last_snapshot"]["frame_source"] == "snapshot_cache"
    assert result["last_snapshot"]["first_frame_ms"] is None
    no_capture.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_spawn_and_cancel_leave_terminal_evidence_and_close_proxy(monkeypatch, cancel):
    from backend.app.services import camera_tls

    close = AsyncMock()
    monkeypatch.setattr(camera_tls, "close_tls_proxy", close)
    entered = asyncio.Event()

    @metrics.observed_stream("rtsp")
    async def source(printer_id=42):
        async with CameraAttempt("synthetic") as attempt:
            attempt.proxy = object()
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise FileNotFoundError("synthetic missing binary")
            yield  # make this an async generator

    stream = source()
    pending = asyncio.create_task(anext(stream))
    await entered.wait()
    if cancel:
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else FileNotFoundError):
        await pending
    close.assert_awaited_once()
    evidence = metrics.for_printer(42)
    assert evidence["end_reason"] == ("client_disconnected" if cancel else "upstream_error")
    assert evidence["attempts_total"] == 1
    assert evidence["first_frame_ms"] is None
    assert evidence["cleanup_ms"] is not None
    assert not metrics._active
