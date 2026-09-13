"""HTTP fan-out integration for the worker-owned external camera relay."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.api.routes import camera as camera_routes
from backend.app.services.external_camera import format_mjpeg_frame

_JPEG = b"\xff\xd8worker-route-test\xff\xd9"


@pytest.mark.asyncio
async def test_worker_external_route_keeps_http_fanout_and_buffers_frames():
    printer_id = 987_651
    calls: list[dict] = []

    class WorkerRuntime:
        async def stream_external(self, **kwargs):
            calls.append(kwargs)
            kwargs["on_frame"](_JPEG)
            yield format_mjpeg_frame(_JPEG)
            await kwargs["disconnect_event"].wait()

    printer = SimpleNamespace(
        external_camera_url="rtsp://operator:secret@camera.example/live",
        external_camera_type="rtsp",
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    key = f"printer-{printer_id}"
    response = await camera_routes._worker_external_stream_response(
        printer=printer,
        printer_id=printer_id,
        request=request,
        fps=5,
        runtime=WorkerRuntime(),
    )
    try:
        chunk = await asyncio.wait_for(anext(response.body_iterator), timeout=1)
        assert _JPEG in chunk
        assert camera_routes._get_cached_snapshot(printer_id) == _JPEG
        assert calls[0]["identity"] == str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"bamdude:printer:{printer_id}:external-camera")
        )
    finally:
        await camera_routes.shutdown_broadcaster(key)
        camera_routes._active_external_streams.discard(printer_id)
        camera_routes._release_printer_frame_state(printer_id)
