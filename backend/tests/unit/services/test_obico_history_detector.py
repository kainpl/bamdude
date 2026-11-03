"""Same bytes as the nonce/ML input, truthful provenance and action isolation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest

from backend.app.services import obico_history as module
from backend.app.services.obico_detection import CapturedFrame, ObicoDetectionService
from backend.app.services.obico_smoothing import WARMUP_FRAMES, PrintState
from backend.tests.unit.services.test_obico_history import history, jpeg  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "case", ["safe-box", "failure-empty", "safe-empty", "off", "queue-failure", "write-failure", "deleted"]
)
async def test_actual_http_inference_saves_nonce_input_and_never_changes_actions(history, monkeypatch, case):
    import backend.app.services.obico_detection as detector
    from backend.app.services.print_run_binding import PrintRunBinding
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(module, "obico_history", history)
    frame_a, frame_b = jpeg(color="red"), jpeg(color="blue")
    source = CapturedFrame(frame_a, "live-buffer", "2026-11-03T01:00:10+00:00", "2026-11-03T01:00:00+00:00", 10)
    service = ObicoDetectionService()
    from fastapi import FastAPI

    from backend.app.api.routes.obico import router

    nonce_app = FastAPI()
    nonce_app.include_router(router, prefix="/api/v1")
    capture = AsyncMock(return_value=source)
    monkeypatch.setattr(service, "_capture_frame", capture)
    actions = AsyncMock()
    monkeypatch.setattr(service, "_dispatch_action", actions)
    run_a = PrintRunBinding(printer_id=1, archive_id=7, queue_item_id=9, origin="dispatch")
    monkeypatch.setattr(printer_manager, "_print_run_bindings", {1: run_a})
    low = case in ("safe-box", "safe-empty", "off", "deleted")
    boxes = [["box", 0.1, [320, 180, 100, 80]]] if case in ("safe-box", "off", "deleted") else []
    if not low:
        state = PrintState()
        for _ in range(WARMUP_FRAMES + 700):
            state.update(1)
        service._states[1] = state
        service._state_keys[1] = "same-name"

    async def handler(request):
        assert request.url.path == "/p/"
        nonce = urlsplit(request.url.params["img"]).path.rsplit("/", 1)[-1]
        async with real_client(transport=httpx.ASGITransport(app=nonce_app), base_url="http://bamdude.test") as reader:
            response = await reader.get(f"/api/v1/obico/cached-frame/{nonce}")
            assert response.status_code == 200 and response.content == frame_a
            assert response.headers["content-type"] == "image/jpeg"
            assert (await reader.get(f"/api/v1/obico/cached-frame/{nonce}")).status_code == 404
        capture.return_value = CapturedFrame(frame_b, "capture-fresh", "2026-11-03T01:00:11+00:00")
        printer_manager._print_run_bindings[1] = PrintRunBinding(printer_id=1, archive_id=8, origin="dispatch")
        if case == "deleted":
            async with history.printer_deletion(1):
                pass  # an old observation must stay fenced even after rollback
        return httpx.Response(200, json={"detections": boxes})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(detector.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    if case == "queue-failure":
        monkeypatch.setattr(history, "offer", lambda **kw: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    if case == "write-failure":
        monkeypatch.setattr(module, "_publish", lambda *a: (_ for _ in ()).throw(OSError("disk unavailable")))
    settings = {
        "save_detections": case != "off",
        "external_url": "http://bamdude.test",
        "ml_url": "http://ml.test",
        "sensitivity": "medium",
        "action": "pause",
    }
    await service._check_printer(1, SimpleNamespace(task_name="same-name"), settings)
    await history._queue.join()
    capture.assert_awaited_once_with(1)
    assert actions.await_count == (0 if low else 1)
    assert service._last_error is None
    page = await history.page(scope=None)
    if case in ("off", "safe-empty", "queue-failure", "write-failure", "deleted"):
        assert page["items"] == []
        if case == "deleted":
            assert not service._history
    else:
        assert len(page["items"]) == 1
        event_id = page["items"][0]["id"]
        assert await history.details(1, event_id, image=True) == frame_a
        detail = await history.details(1, event_id)
        assert detail["facts"]["frame_source"] == "live-buffer"
        assert detail["facts"]["buffer_received_at"] == "2026-11-03T01:00:00+00:00"
        assert detail["facts"]["buffer_age_seconds"] == 10
        assert detail["facts"]["captured_at"] is None
        assert detail["facts"]["association"] == "unknown"
        assert detail["facts"]["archive_id"] is None
        assert detail["facts"]["action_requested"] == (not low)


async def test_buffer_bytes_arrival_and_generation_are_one_snapshot(monkeypatch):
    from backend.app.api.routes import camera
    from backend.app.services.camera_runtime import CameraCaptureRequest

    request = CameraCaptureRequest.builtin(ip_address="127.0.0.1", access_code="fake", model="A1")
    frame_a, frame_b = jpeg(), jpeg(color="blue")
    monkeypatch.setitem(camera._active_worker_streams, 1, ("producer-a", "builtin"))
    monkeypatch.setitem(camera._last_frames, 1, frame_a)
    monkeypatch.setitem(camera._last_frame_times, 1, 123.0)
    monkeypatch.setitem(camera._live_frame_metadata, 1, (10.0, "producer-a", request))
    frozen = camera.live_frame_observation(1)
    monkeypatch.setitem(camera._last_frames, 1, frame_b)
    monkeypatch.setitem(camera._last_frame_times, 1, 456.0)
    monkeypatch.setitem(camera._live_frame_metadata, 1, (20.0, "producer-b", request))
    assert frozen == (True, frame_a, 123.0, 10.0)
    # A stale producer cannot lend its timestamp to the successor's bytes.
    assert camera.live_frame_observation(1) == (True, frame_b, None, None)
