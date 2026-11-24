"""Discover only supported USB devices without blocking the HTTP loop."""

import asyncio
from threading import Event
from unittest.mock import patch

import pytest

from backend.app.core.permissions import Permission
from backend.app.services.external_camera import list_usb_cameras
from backend.tests.integration.test_api_key_owner_authority import _key, _user

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_list_filters_to_canonical_supported_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.services.external_camera.shutil.which", lambda _name: None)
    for name in ("video10", "video0", "video02", "video2", "video4", "video1", "video100", "video-meta"):
        (tmp_path / name).touch()

    assert [item["device"] for item in list_usb_cameras(tmp_path)] == [
        "/dev/video0",
        "/dev/video2",
        "/dev/video4",
        "/dev/video10",
    ]


async def test_usb_listing_keeps_the_event_loop_running(async_client):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    released_during_probe = []

    def probe():
        loop.call_soon_threadsafe(entered.set)
        released_during_probe.append(release.wait(timeout=3))
        return [{"device": "/dev/video0", "name": "Camera"}]

    with patch("backend.app.services.external_camera.list_usb_cameras", probe):
        request = asyncio.create_task(async_client.get("/api/v1/printers/usb-cameras"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not request.done(), "the worker should still be waiting while the event loop runs"
        finally:
            release.set()
            response = await request

    assert released_during_probe == [True], "the event loop must release the probe before its timeout"
    assert response.status_code == 200, response.text
    assert response.json() == {"cameras": [{"device": "/dev/video0", "name": "Camera"}]}


async def test_usb_availability_checks_saved_nodes_without_exposing_sources(async_client, db_session, printer_factory):
    def setup(device):
        return {
            "external_camera_enabled": True,
            "external_camera_type": "usb",
            "external_camera_url": device,
        }

    found = await printer_factory(**setup("/dev/video02"))
    missing = await printer_factory(**setup("/dev/video4"))
    foreign = await printer_factory(**setup("rtsp://private:password@private.example/live"))
    devices = [{"device": "/dev/video2", "name": "Camera"}]
    with patch("backend.app.services.external_camera.list_usb_cameras", return_value=devices):
        response = await async_client.get("/api/v1/printers/usb-cameras", params={"include_configured": "true"})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "cameras": devices,
            "configured_devices_found": {str(found.id): True, str(missing.id): False, str(foreign.id): False},
        }
        owner = await _user(db_session, "usb-viewer", [Permission.PRINTERS_READ.value])
        raw, _ = await _key(db_session, owner=owner, printer_ids=[missing.id])
        scoped = await async_client.get(
            "/api/v1/printers/usb-cameras", params={"include_configured": "true"}, headers={"X-API-Key": raw}
        )

    assert scoped.status_code == 200, scoped.text
    assert scoped.json() == {"cameras": devices, "configured_devices_found": {str(missing.id): False}}
