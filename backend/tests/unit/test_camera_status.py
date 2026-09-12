"""The camera-status endpoint exposes non-secret support evidence."""

from types import SimpleNamespace

import pytest

from backend.app.api.routes import camera as camera_routes


@pytest.mark.asyncio
async def test_status_reports_builtin_source_and_fanout_subscribers(monkeypatch):
    printer_id = 42
    monkeypatch.setattr(
        camera_routes,
        "_active_streams",
        {f"{printer_id}-fanout-test": SimpleNamespace(returncode=None)},
    )
    monkeypatch.setattr(camera_routes, "_active_chamber_streams", {})
    monkeypatch.setattr(camera_routes, "_active_external_streams", set())
    monkeypatch.setattr(camera_routes, "_last_frames", {printer_id: b"frame"})
    monkeypatch.setattr(camera_routes, "_last_frame_times", {printer_id: 100.0})
    monkeypatch.setattr(camera_routes, "_stream_start_times", {printer_id: 90.0})
    monkeypatch.setattr(camera_routes, "get_subscriber_count", lambda key: 3 if key == "printer-42" else 0)
    monkeypatch.setattr(camera_routes.time, "time", lambda: 105.0)

    status = await camera_routes.camera_status(printer_id, None)

    assert status["active"] is True
    assert status["source"] == "rtsp"
    assert status["subscribers"] == 3
    assert status["seconds_since_frame"] == 5.0
    assert status["stalled"] is False
