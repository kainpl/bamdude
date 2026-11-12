"""Exercise the actual relay producer, buffer accessor, and fresh plate capture."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.app.api.routes import camera
from backend.app.services import plate_detection as pd
from backend.app.services.camera_runtime import CameraCaptureRequest
from backend.app.services.external_camera import format_mjpeg_frame
from backend.tests.unit.live_media_harness import media_runtime, request as make_request, serve  # noqa: F401

# A frame's wait: a loaded CI runner delivered the first one at 1.96 s (CI 39126040756), past the 1 s it had.
FRAME_WAIT_SECONDS = 2.5


@pytest_asyncio.fixture
async def live(monkeypatch, media_runtime):
    pid = 987_660
    old = b"\xff\xd8old-frame\xff\xd9"
    calls = []
    observer = []
    responses = []

    class Runtime:
        async def stream_builtin(self, **kwargs):
            calls.append(kwargs)
            observer.append(kwargs["on_frame"])
            kwargs["on_frame"](old)
            yield format_mjpeg_frame(old)
            await kwargs["disconnect_event"].wait()

        stream_external = stream_builtin

    async def start(external=False):
        printer = SimpleNamespace(
            id=pid,
            connector_identity="test-printer-incarnation",
            ip_address="192.0.2.60",
            access_code="synthetic",
            model="A1M",
            external_camera_url="http://camera-a.invalid",
            external_camera_type="mjpeg",
            external_camera_snapshot_url=None,
        )
        request = make_request()
        response = await camera._worker_stream_response(
            printer=printer,
            printer_id=pid,
            request=request,
            fps=5,
            runtime=Runtime(),
            builtin=not external,
        )
        task, chunks, disconnect = serve(response, request)
        responses.append((task, disconnect))
        assert old in (await asyncio.wait_for(chunks.get(), FRAME_WAIT_SECONDS))["body"]
        return response

    capture = AsyncMock()
    monkeypatch.setattr("backend.app.services.camera_runtime.capture", capture)
    yield pid, start, observer, calls, capture, old
    for task, disconnect in responses:
        disconnect.set()
        await asyncio.wait_for(task, 2)
    await camera.shutdown_broadcaster(f"printer-{pid}")
    camera._active_worker_streams.pop(pid, None)
    camera._release_printer_frame_state(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("wallclock", [1.0, 4_000_000_000.0])
async def test_next_published_frame_required_despite_wallclock_jumps(live, monkeypatch, wallclock):
    pid, start, observer, calls, capture, old = live
    await start()
    monkeypatch.setattr(time, "time", lambda: wallclock)
    task = asyncio.create_task(pd.capture_camera_image(pid, "192.0.2.60", "synthetic", "A1M", fresh=True))
    await asyncio.sleep(0.05)
    assert not task.done(), "a pre-check live frame must not pass freshness"
    new = b"\xff\xd8next-frame\xff\xd9"
    observer[0](new)
    assert (await asyncio.wait_for(task, FRAME_WAIT_SECONDS))[0] == new
    assert camera._last_frame_times[pid] == wallclock
    assert len(calls) == 1
    capture.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["stalled", "builtin-to-external", "external-url", "snapshot-url", "external-to-builtin"]
)
async def test_stalled_or_mismatched_source_defers_without_competing_reader(live, change):
    pid, start, observer, calls, capture, old = live
    external = change in ("external-url", "snapshot-url", "external-to-builtin")
    await start(external)
    use_external = change != "external-to-builtin" and (external or change == "builtin-to-external")
    url = "http://camera-b.invalid" if change == "external-url" else "http://camera-a.invalid"
    task = asyncio.create_task(
        pd.capture_camera_image(
            pid,
            "192.0.2.60",
            "synthetic",
            "A1M",
            use_external=use_external,
            external_camera_url=url,
            external_camera_type="mjpeg",
            external_camera_snapshot_url="http://snapshot-b.invalid" if change == "snapshot-url" else None,
            fresh=True,
        )
    )
    await asyncio.sleep(0.02)
    if change != "stalled":
        observer[0](b"\xff\xd8wrong-source-new-frame\xff\xd9")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(task, 0.2)
    capture.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_matching_external_next_frame_reuses_actual_producer(live):
    pid, start, observer, calls, capture, old = live
    await start(True)
    task = asyncio.create_task(
        pd.capture_camera_image(
            pid,
            "192.0.2.60",
            "synthetic",
            "A1M",
            use_external=True,
            external_camera_url="http://camera-a.invalid",
            external_camera_type="mjpeg",
            fresh=True,
        )
    )
    await asyncio.sleep(0.02)
    observer[0](b"new-external-frame")
    assert (await asyncio.wait_for(task, FRAME_WAIT_SECONDS))[0] == b"new-external-frame"
    capture.assert_not_awaited()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, ("successor", "chamber_image")])
async def test_stream_end_or_replacement_does_not_switch_waiting_capture(live, replacement):
    pid, start, observer, calls, capture, old = live
    await start()
    task = asyncio.create_task(pd.capture_camera_image(pid, "192.0.2.60", "synthetic", "A1M", fresh=True))
    await asyncio.sleep(0.02)
    if replacement is None:
        camera._active_worker_streams.pop(pid)
    else:
        camera._active_worker_streams[pid] = replacement
    observer[0](b"departing-producer-frame")
    assert camera._last_frames[pid] == old
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(task, 0.2)
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_accessor_rejects_frame_without_producer_metadata(live):
    pid, start, observer, calls, capture, old = live
    await start()
    camera._live_frame_metadata.pop(pid)
    expected = CameraCaptureRequest.builtin(ip_address="192.0.2.60", access_code="synthetic", model="A1M")
    assert camera.live_frame_for_capture(pid, not_before=time.monotonic(), expected_source=expected) == (True, None)
    assert camera.live_frame_for_capture(pid) == (True, old)


@pytest.mark.asyncio
async def test_stalled_producer_returns_unavailable_at_its_bounded_deadline(live):
    pid, start, observer, calls, capture, old = live
    await start()
    began = time.monotonic()
    frame, reason = await asyncio.wait_for(
        pd.capture_camera_image(pid, "192.0.2.60", "synthetic", "A1M", fresh=True), 12
    )
    assert frame is None
    assert "buffer" in reason
    assert 9.9 <= time.monotonic() - began < 12
    capture.assert_not_awaited()
