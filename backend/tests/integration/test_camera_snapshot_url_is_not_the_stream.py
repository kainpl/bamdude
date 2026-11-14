"""A "snapshot URL" that is the camera's stream URL is saved as no snapshot URL (review M7).

A camera with its own snapshot URL answers every one-shot picture from it, also while watched. When that
address IS the stream's, each picture would be a second reader of the stream — and a single-client MJPEG
source drops the viewer for it. The save drops the duplicate; ``camera_runtime.capture`` ignores one that
reached a row some other way (``test_camera_own_still``).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.models.camera import Camera
from backend.app.models.printer import Printer
from backend.app.services.camera_credentials import same_camera_address

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

STREAM = "http://192.0.2.50/stream?src=cam"
STILL = "http://192.0.2.50/frame.jpg"


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        (STREAM, STREAM, True),
        (STREAM, f"  {STREAM}  ", True),
        (STREAM, "HTTP://192.0.2.50/stream?src=cam", True),
        (STREAM, STREAM + "#frame", True),
        (STREAM, STILL, False),
        (STREAM, "http://192.0.2.50/stream?src=other", False),
        (STREAM, "http://192.0.2.50/Stream?src=cam", False),
        (STREAM, None, False),
        (None, None, False),
        ("", "", False),
    ],
)
async def test_same_camera_address(first, second, same):
    assert same_camera_address(first, second) is same


class TestAStandaloneCamera:
    async def test_created_with_the_stream_as_its_snapshot_url(self, async_client: AsyncClient, db_session):
        body = {"name": "Shelf", "camera_type": "mjpeg", "url": STREAM, "snapshot_url": STREAM}

        created = await async_client.post("/api/v1/cameras/", json=body)

        assert created.status_code == 201, created.text
        assert created.json()["snapshot_url_configured"] is False
        assert (await db_session.get(Camera, created.json()["id"])).snapshot_url is None

    async def test_a_real_snapshot_url_is_kept(self, async_client: AsyncClient, db_session):
        body = {"name": "Shelf", "camera_type": "mjpeg", "url": STREAM, "snapshot_url": STILL}

        created = await async_client.post("/api/v1/cameras/", json=body)

        assert (await db_session.get(Camera, created.json()["id"])).snapshot_url == STILL

    @pytest.mark.parametrize(
        "change", [{"snapshot_url": STREAM.upper().replace("STREAM?SRC=CAM", "stream?src=cam")}, {"url": STILL}]
    )
    async def test_an_edit_that_makes_them_the_same(self, async_client: AsyncClient, db_session, change):
        body = {"name": "Shelf", "camera_type": "mjpeg", "url": STREAM, "snapshot_url": STILL}
        camera_id = (await async_client.post("/api/v1/cameras/", json=body)).json()["id"]

        edited = await async_client.patch(f"/api/v1/cameras/{camera_id}", json=change)

        assert edited.status_code == 200, edited.text
        assert edited.json()["snapshot_url_configured"] is False
        db_session.expire_all()
        assert (await db_session.get(Camera, camera_id)).snapshot_url is None


class TestAPrintersExternalCamera:
    @pytest.mark.parametrize(
        "change", [{"external_camera_snapshot_url": STREAM + "#x"}, {"external_camera_url": STILL}]
    )
    async def test_an_edit_that_makes_them_the_same(
        self, async_client: AsyncClient, db_session, printer_factory, change
    ):
        printer = await printer_factory(
            external_camera_url=STREAM,
            external_camera_snapshot_url=STILL,
            external_camera_type="mjpeg",
            external_camera_enabled=True,
        )
        printer_id = printer.id

        edited = await async_client.patch(f"/api/v1/printers/{printer_id}", json=change)

        assert edited.status_code == 200, edited.text
        assert edited.json()["external_camera_snapshot_url_configured"] is False
        db_session.expire_all()
        row = await db_session.get(Printer, printer_id)
        assert row.external_camera_snapshot_url is None and row.external_camera_url

    async def test_an_unrelated_edit_keeps_a_real_snapshot_url(
        self, async_client: AsyncClient, db_session, printer_factory
    ):
        printer = await printer_factory(
            external_camera_url=STREAM,
            external_camera_snapshot_url=STILL,
            external_camera_type="mjpeg",
            external_camera_enabled=True,
        )
        printer_id = printer.id

        assert (await async_client.patch(f"/api/v1/printers/{printer_id}", json={"name": "Renamed"})).status_code == 200

        db_session.expire_all()
        assert (await db_session.get(Printer, printer_id)).external_camera_snapshot_url == STILL

    async def test_created_with_the_stream_as_its_snapshot_url(
        self, async_client: AsyncClient, db_session, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from backend.app.api.routes import printers

        monkeypatch.setattr(printers.printer_manager, "test_connection", AsyncMock(return_value={"success": True}))
        monkeypatch.setattr(printers.printer_manager, "connect_printer", AsyncMock())
        body = {
            "name": "Camera create",
            "serial_number": "CAMERA_SAME_URL",
            "ip_address": "192.0.2.15",
            "access_code": "12345678",
            "external_camera_url": STREAM,
            "external_camera_snapshot_url": STREAM,
            "external_camera_type": "mjpeg",
        }

        created = await async_client.post("/api/v1/printers/", json=body)

        assert created.status_code == 200, created.text
        assert (await db_session.get(Printer, created.json()["id"])).external_camera_snapshot_url is None
