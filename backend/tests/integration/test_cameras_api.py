"""Cameras that belong to no printer: CRUD, the stream, the frame, the kiosk list.

Spec 60-specs/standalone-cameras-spec §6. The point of most of these is what a
camera is NOT: it is not a printer, so it never reaches the printer registries;
its kiosk list never carries a URL, because an RTSP camera's credentials live
inside one; and a camera the operator switched off serves nothing.
"""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from backend.app.services.camera_metrics import CameraCaptureResult

MJPEG = {"name": "Shelf", "camera_type": "mjpeg", "url": "http://192.0.2.50/stream"}


@pytest_asyncio.fixture(autouse=True)
async def _clear_camera_registries():
    from backend.app.api.routes import cameras

    for registry in (
        cameras._last_frames,
        cameras._last_frame_times,
        cameras._snapshot_frames,
        cameras._snapshot_frame_times,
    ):
        registry.clear()
    yield
    for registry in (
        cameras._last_frames,
        cameras._last_frame_times,
        cameras._snapshot_frames,
        cameras._snapshot_frame_times,
    ):
        registry.clear()


@pytest_asyncio.fixture
async def stream_token(async_client: AsyncClient):
    """The same short-lived token an <img> tag carries."""
    from backend.app.core.auth import create_camera_stream_token

    return await create_camera_stream_token()


class TestCameraCrud:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_list_update_delete(self, async_client: AsyncClient):
        created = await async_client.post("/api/v1/cameras/", json=MJPEG)
        assert created.status_code == 201, created.text
        camera_id = created.json()["id"]
        assert created.json()["enabled"] is True
        assert created.json()["location_id"] is None

        listed = await async_client.get("/api/v1/cameras/")
        assert listed.status_code == 200
        assert [c["name"] for c in listed.json()] == ["Shelf"]
        # The settings list carries the URL — it is the screen where it is typed.
        assert listed.json()[0]["url"] == MJPEG["url"]

        patched = await async_client.patch(f"/api/v1/cameras/{camera_id}", json={"name": "Lobby", "rotation": 180})
        assert patched.status_code == 200
        assert patched.json()["name"] == "Lobby"
        assert patched.json()["rotation"] == 180

        deleted = await async_client.delete(f"/api/v1/cameras/{camera_id}")
        assert deleted.status_code == 200
        assert (await async_client.get("/api/v1/cameras/")).json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_name_belongs_to_one_camera(self, async_client: AsyncClient):
        """The wall tile, the location button and the window title all say the name."""
        assert (await async_client.post("/api/v1/cameras/", json=MJPEG)).status_code == 201
        duplicate = await async_client.post("/api/v1/cameras/", json={**MJPEG, "url": "http://192.0.2.51/s"})
        assert duplicate.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_bad_url_is_refused_on_write_and_a_usb_path_is_not_a_url(self, async_client: AsyncClient):
        bad = await async_client.post("/api/v1/cameras/", json={**MJPEG, "url": "not a url"})
        assert bad.status_code == 422

        # USB sources are device paths; pushing them through a URL parser would
        # reject every one of them.
        usb = await async_client.post(
            "/api/v1/cameras/", json={"name": "Bench", "camera_type": "usb", "url": "/dev/video0"}
        )
        assert usb.status_code == 201

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rotation_and_type_are_closed_sets(self, async_client: AsyncClient):
        assert (await async_client.post("/api/v1/cameras/", json={**MJPEG, "rotation": 45})).status_code == 422
        assert (await async_client.post("/api/v1/cameras/", json={**MJPEG, "camera_type": "onvif"})).status_code == 422
        assert (await async_client.post("/api/v1/cameras/", json={**MJPEG, "name": "  "})).status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_url_change_is_validated_against_the_stored_type(self, async_client: AsyncClient):
        """Either half of the pair may be the one that changed, so both are re-read."""
        usb = await async_client.post(
            "/api/v1/cameras/", json={"name": "Bench", "camera_type": "usb", "url": "/dev/video0"}
        )
        camera_id = usb.json()["id"]
        # Still USB: a device path stays valid.
        assert (
            await async_client.patch(f"/api/v1/cameras/{camera_id}", json={"url": "/dev/video1"})
        ).status_code == 200
        # Now MJPEG: the same device path is not a URL any more.
        refused = await async_client.patch(f"/api/v1/cameras/{camera_id}", json={"camera_type": "mjpeg"})
        assert refused.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_unknown_location_is_a_404_and_a_known_one_comes_back_named(
        self, async_client: AsyncClient, db_session
    ):
        assert (await async_client.post("/api/v1/cameras/", json={**MJPEG, "location_id": 9999})).status_code == 404

        from backend.app.models.printer_location import PrinterLocation
        from backend.app.services.printer_location_service import location_key

        place = PrinterLocation(name="Room A", name_key=location_key("Room A"))
        db_session.add(place)
        await db_session.commit()
        await db_session.refresh(place)

        created = await async_client.post("/api/v1/cameras/", json={**MJPEG, "location_id": place.id})
        assert created.status_code == 201
        assert created.json()["location_name"] == "Room A"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_location_still_holding_a_camera_refuses_to_be_deleted(self, async_client: AsyncClient, db_session):
        """The same answer an adopted sensor gets — SQLite never enforces the FK itself."""
        from backend.app.models.printer_location import PrinterLocation
        from backend.app.services.printer_location_service import location_key

        place = PrinterLocation(name="Room B", name_key=location_key("Room B"))
        db_session.add(place)
        await db_session.commit()
        await db_session.refresh(place)

        assert (await async_client.post("/api/v1/cameras/", json={**MJPEG, "location_id": place.id})).status_code == 201
        refused = await async_client.delete(f"/api/v1/printer-locations/{place.id}")
        assert refused.status_code == 409
        assert "camera" in refused.json()["detail"].lower() or "камер" in refused.json()["detail"].lower()


class TestCameraStream:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_viewers_share_one_upstream(self, async_client: AsyncClient, stream_token):
        """One camera, one connection — the rule the printers' built-in cameras follow."""
        camera_id = (await async_client.post("/api/v1/cameras/", json=MJPEG)).json()["id"]
        factories: list = []

        class FakeBroadcaster:
            subscriber_count = 1

            async def subscribe(self):
                import asyncio

                return asyncio.Queue()

        async def fake_get_or_create(key, factory):
            factories.append(key)
            return FakeBroadcaster()

        async def fake_iter(broadcaster, queue, *, is_disconnected, on_unsubscribe):
            yield b"--frame\r\n"

        with (
            patch("backend.app.api.routes.cameras.get_or_create_broadcaster", fake_get_or_create),
            patch("backend.app.api.routes.cameras.iter_subscriber", fake_iter),
        ):
            first = await async_client.get(f"/api/v1/cameras/{camera_id}/stream", params={"token": stream_token})
            second = await async_client.get(f"/api/v1/cameras/{camera_id}/stream", params={"token": stream_token})

        assert first.status_code == second.status_code == 200
        # Both viewers asked the registry for the SAME key, which is what makes
        # it one upstream rather than two.
        assert factories == [f"camera-{camera_id}", f"camera-{camera_id}"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_stream_needs_the_token_and_a_switched_on_camera(self, async_client: AsyncClient, stream_token):
        camera_id = (await async_client.post("/api/v1/cameras/", json=MJPEG)).json()["id"]

        unauthenticated = await async_client.get(f"/api/v1/cameras/{camera_id}/stream")
        assert unauthenticated.status_code == 401

        await async_client.patch(f"/api/v1/cameras/{camera_id}", json={"enabled": False})
        off = await async_client.get(f"/api/v1/cameras/{camera_id}/stream", params={"token": stream_token})
        assert off.status_code == 404


class TestCameraSnapshot:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_live_frame_is_reused_instead_of_opening_a_second_reader(
        self, async_client: AsyncClient, stream_token
    ):
        """Many of these sources allow exactly one reader; a viewer must not be kicked off."""
        from backend.app.api.routes import cameras

        camera_id = (await async_client.post("/api/v1/cameras/", json=MJPEG)).json()["id"]
        cameras._publish_frame(camera_id, b"\xff\xd8live")

        with patch("backend.app.services.camera_runtime.capture", new_callable=AsyncMock) as capture:
            response = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params={"token": stream_token})

        assert response.status_code == 200
        assert response.content == b"\xff\xd8live"
        capture.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_without_a_viewer_one_capture_serves_repeated_polls(self, async_client: AsyncClient, stream_token):
        camera_id = (await async_client.post("/api/v1/cameras/", json=MJPEG)).json()["id"]

        with patch("backend.app.services.camera_runtime.capture", new_callable=AsyncMock) as capture:
            capture.return_value = CameraCaptureResult(b"\xff\xd8fresh", "fresh")
            first = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params={"token": stream_token})
            second = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params={"token": stream_token})

        assert first.content == second.content == b"\xff\xd8fresh"
        capture.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_capture_that_returns_nothing_is_a_503(self, async_client: AsyncClient, stream_token):
        camera_id = (await async_client.post("/api/v1/cameras/", json=MJPEG)).json()["id"]

        with patch("backend.app.services.camera_runtime.capture", new_callable=AsyncMock) as capture:
            capture.return_value = CameraCaptureResult(None, None)
            response = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params={"token": stream_token})

        assert response.status_code == 503


class TestKioskFeed:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_kiosk_list_names_the_camera_and_never_its_url(self, async_client: AsyncClient, db_session):
        """An RTSP camera's credentials live inside its URL, and a kiosk URL is a sticky note."""
        from backend.app.models.user import User
        from backend.app.services.long_lived_tokens import create_token

        rtsp = {"name": "Lobby", "camera_type": "rtsp", "url": "rtsp://user:secret@192.0.2.60:554/stream"}
        assert (await async_client.post("/api/v1/cameras/", json=rtsp)).status_code == 201
        off = {"name": "Retired", "camera_type": "mjpeg", "url": "http://192.0.2.61/s", "enabled": False}
        assert (await async_client.post("/api/v1/cameras/", json=off)).status_code == 201

        user = (await db_session.execute(__import__("sqlalchemy").select(User))).scalars().first()
        created = await create_token(db_session, user_id=user.id, name="TV", expires_in_days=7, scope="camwall")

        response = await async_client.get("/api/v1/camwall/cameras", params={"token": created.plaintext})
        assert response.status_code == 200
        payload = response.json()
        assert [c["name"] for c in payload] == ["Lobby"], "a switched-off camera is not on a kiosk wall"
        assert "url" not in payload[0]
        assert set(payload[0]) == {"id", "name", "rotation", "location_id"}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_kiosk_list_refuses_a_stream_scoped_token(self, async_client: AsyncClient, db_session):
        from backend.app.models.user import User
        from backend.app.services.long_lived_tokens import create_token

        user = (await db_session.execute(__import__("sqlalchemy").select(User))).scalars().first()
        wrong = await create_token(db_session, user_id=user.id, name="Frigate", expires_in_days=7)

        response = await async_client.get("/api/v1/camwall/cameras", params={"token": wrong.plaintext})
        assert response.status_code == 401
