"""HTTP fan-out integration for the worker-owned external camera relay."""

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.api.routes import camera as camera_routes
from backend.app.services.external_camera import format_mjpeg_frame

_JPEG = b"\xff\xd8worker-route-test\xff\xd9"


@pytest.mark.asyncio
async def test_worker_external_route_keeps_http_fanout_and_buffers_frames(caplog):
    caplog.set_level(logging.INFO, logger="backend.app.api.routes.camera")
    printer_id = 987_651
    calls: list[dict] = []

    class WorkerRuntime:
        async def stream_external(self, **kwargs):
            calls.append(kwargs)
            kwargs["on_frame"](_JPEG)
            yield format_mjpeg_frame(_JPEG)
            await asyncio.sleep(0)
            kwargs["on_frame"](_JPEG)
            yield format_mjpeg_frame(_JPEG)
            await kwargs["disconnect_event"].wait()

    printer = SimpleNamespace(
        external_camera_url="rtsp://operator:secret@camera.example/live",
        external_camera_type="rtsp",
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    key = f"printer-{printer_id}"
    response = await camera_routes._worker_stream_response(
        printer=printer,
        printer_id=printer_id,
        request=request,
        fps=5,
        runtime=WorkerRuntime(),
    )
    try:
        chunk = await asyncio.wait_for(anext(response.body_iterator), timeout=1)
        assert _JPEG in chunk
        assert _JPEG in await asyncio.wait_for(anext(response.body_iterator), timeout=1)
        assert camera_routes._get_cached_snapshot(printer_id) == _JPEG
        assert calls[0]["identity"] == str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"bamdude:printer:{printer_id}:external-camera")
        )
    finally:
        await camera_routes.shutdown_broadcaster(key)
        camera_routes._active_worker_streams.pop(printer_id, None)
        camera_routes._release_printer_frame_state(printer_id)
    assert caplog.text.count("Camera worker relay started:") == 1
    assert caplog.text.count("Camera worker relay first frame:") == 1
    assert caplog.text.count("Camera worker relay ended:") == 1
    assert f"printer={printer_id}" in caplog.text
    assert "frames=2" in caplog.text
    assert "reason=viewers_gone" in caplog.text
    assert printer.external_camera_url not in caplog.text
    assert "operator:secret" not in caplog.text


@pytest.mark.asyncio
async def test_worker_builtin_route_uses_stable_builtin_identity():
    printer_id = 987_652
    calls: list[dict] = []

    class WorkerRuntime:
        async def stream_builtin(self, **kwargs):
            calls.append(kwargs)
            kwargs["on_frame"](_JPEG)
            yield format_mjpeg_frame(_JPEG)
            await kwargs["disconnect_event"].wait()

    printer = SimpleNamespace(
        ip_address="192.0.2.52",
        access_code="12345678",
        model="P1S",
        external_camera_url=None,
        external_camera_type=None,
    )
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    key = f"printer-{printer_id}"
    response = await camera_routes._worker_stream_response(
        printer=printer,
        printer_id=printer_id,
        request=request,
        fps=5,
        runtime=WorkerRuntime(),
        builtin=True,
    )
    try:
        assert _JPEG in await asyncio.wait_for(anext(response.body_iterator), timeout=1)
        assert calls[0]["identity"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"bamdude:printer:{printer_id}:builtin"))
        assert camera_routes.is_stream_active(printer_id)
        assert (await camera_routes.camera_status(printer_id, None))["source"] == "chamber_image"
    finally:
        await camera_routes.shutdown_broadcaster(key)
        camera_routes._active_worker_streams.pop(printer_id, None)
        camera_routes._release_printer_frame_state(printer_id)
