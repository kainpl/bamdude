"""B camera endpoints are write-only and saved probes require editor rights."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from backend.app.core.auth import create_access_token
from backend.app.models.group import Group
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.user import User

STREAM = "http://camera-user:CANARY_PASSWORD@192.0.2.9/live?key=CANARY_STREAM"
SNAPSHOT = "http://snapshot-user:CANARY_SNAPSHOT_PASSWORD@192.0.2.10/frame?token=CANARY_SNAPSHOT"


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
async def test_camera_urls_never_return_in_any_read_projection(role, async_client, db_session, printer_factory):
    printer = await printer_factory(
        external_camera_url=STREAM,
        external_camera_snapshot_url=SNAPSHOT,
        external_camera_type="mjpeg",
        external_camera_enabled=True,
    )
    if role != "admin":
        group = Group(
            name="Camera " + role,
            permissions=["printers:read", "camera:view"] + (["printers:update"] if role == "editor" else []),
        )
        user = User(username="camera_" + role, role="user", is_active=True, groups=[group])
        db_session.add(user)
        await db_session.commit()
        async_client.headers["Authorization"] = "Bearer " + create_access_token({"sub": user.username})
    for door in ("/api/v1/printers/", f"/api/v1/printers/{printer.id}"):
        result = await async_client.get(door)
        assert result.status_code == 200, result.text
        assert "CANARY" not in result.text
        item = result.json()[0] if isinstance(result.json(), list) else result.json()
        assert "external_camera_url" not in item and "external_camera_snapshot_url" not in item
        assert item["external_camera_url_configured"] and item["external_camera_snapshot_url_configured"]
        assert item["external_camera_host"] == "192.0.2.9"


async def test_rename_actions_and_patch_projection_preserve_private_camera_config(
    async_client, db_session, printer_factory
):
    printer = await printer_factory(
        external_camera_url=STREAM,
        external_camera_snapshot_url=SNAPSHOT,
        external_camera_type="mjpeg",
        external_camera_enabled=True,
    )
    door = f"/api/v1/printers/{printer.id}"
    result = await async_client.patch(door, json={"name": "Renamed", "secret_actions": {"external_camera_url": "keep"}})
    assert result.status_code == 200 and "CANARY" not in result.text
    await db_session.refresh(printer)
    assert printer.external_camera_url == STREAM and printer.external_camera_snapshot_url == SNAPSHOT
    for body in (
        {"secret_actions": {"external_camera_url": "keep"}, "external_camera_url": "CANARY_CONFLICT"},
        {"secret_actions": {"external_camera_url": "replace"}, "external_camera_url": ""},
        {"secret_actions": {"name": "keep"}},
    ):
        result = await async_client.patch(door, json=body)
        assert result.status_code == 422 and "CANARY" not in result.text
    result = await async_client.patch(door, json={"secret_actions": {"external_camera_url": "clear"}})
    assert result.status_code == 400
    result = await async_client.patch(
        door, json={"external_camera_enabled": False, "secret_actions": {"external_camera_url": "clear"}}
    )
    assert result.status_code == 200 and not result.json()["external_camera_url_configured"]
    result = await async_client.patch(door, json={"secret_actions": {"external_camera_url": "keep"}})
    assert result.status_code == 400
    replacement = "http://new.invalid/actual***credential"
    result = await async_client.patch(
        door, json={"external_camera_url": replacement, "secret_actions": {"external_camera_url": "replace"}}
    )
    assert result.status_code == 200 and replacement not in result.text
    await db_session.refresh(printer)
    assert printer.external_camera_url == replacement


async def test_camera_draft_authority_separate_saved_stream_snapshot_and_no_io_transaction(
    async_client, db_session, printer_factory, monkeypatch
):
    printer = await printer_factory(
        external_camera_url=STREAM,
        external_camera_snapshot_url=SNAPSHOT,
        external_camera_type="mjpeg",
        external_camera_enabled=True,
    )
    from backend.app.api.routes import camera
    from backend.app.services import camera_runtime

    sent = []
    request_sessions = []
    original_lookup = camera.get_printer_or_404

    async def lookup(printer_id, db):
        request_sessions.append(db)
        return await original_lookup(printer_id, db)

    monkeypatch.setattr(camera, "get_printer_or_404", lookup)

    async def probe(url, kind):
        # StaticPool cannot prove lock release by opening another session:
        # check the actual request transaction instead.
        assert not request_sessions[-1].in_transaction()
        sent.append((url, kind))
        return {"success": False, "error": "peer echoed " + url}

    monkeypatch.setattr(camera_runtime, "test_external_connection", probe)
    door = f"/api/v1/printers/{printer.id}/camera/external/test"
    for source, expected, kind in (("stream", STREAM, "mjpeg"), ("snapshot", SNAPSHOT, "snapshot")):
        result = await async_client.post(door, json={"source": source})
        assert result.status_code == 200 and "CANARY" not in result.text
        assert sent[-1] == (expected, kind)
    group = Group(name="Camera readonly", permissions=["printers:read", "camera:view"])
    user = User(username="camera_readonly", role="user", is_active=True, groups=[group])
    db_session.add(user)
    await db_session.commit()
    async_client.headers["Authorization"] = "Bearer " + create_access_token({"sub": user.username})
    count = len(sent)
    result = await async_client.post(door, json={"source": "snapshot"})
    assert result.status_code == 403 and len(sent) == count
    result = await async_client.post(door, json={"url": "http://new.invalid/CANARY_DRAFT", "camera_type": "snapshot"})
    assert result.status_code == 200 and "CANARY" not in result.text
    assert sent[-1] == ("http://new.invalid/CANARY_DRAFT", "snapshot")
    await db_session.refresh(printer)
    assert printer.external_camera_url == STREAM and printer.external_camera_snapshot_url == SNAPSHOT


async def test_reset_preserves_the_credential_anchor(async_client, db_session):
    db_session.add(Settings(key="mqtt_password", value="CANARY_RESET"))
    await db_session.commit()
    result = await async_client.post("/api/v1/settings/reset")
    assert result.status_code == 200, result.text
    assert "CANARY" not in result.text
    assert await db_session.scalar(select(Settings.value).where(Settings.key == "_credential_config_guard")) == "1"
    assert await db_session.scalar(select(Settings.value).where(Settings.key == "mqtt_password")) is None


async def test_camera_create_uses_explicit_credentials_but_its_response_is_opaque(
    async_client, db_session, monkeypatch
):
    from backend.app.api.routes import printers

    probe = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(printers.printer_manager, "test_connection", probe)
    monkeypatch.setattr(printers.printer_manager, "connect_printer", AsyncMock())
    body = {
        "name": "Camera create",
        "serial_number": "CAMERA_CREATE",
        "ip_address": "192.0.2.15",
        "access_code": "12345678",
        "external_camera_url": STREAM,
        "external_camera_snapshot_url": SNAPSHOT,
        "external_camera_type": "mjpeg",
    }
    invalid = await async_client.post(
        "/api/v1/printers/",
        json={
            **{key: value for key, value in body.items() if key != "external_camera_url"},
            "secret_actions": {"external_camera_url": "keep"},
        },
    )
    assert invalid.status_code == 400
    probe.assert_not_awaited()
    created = await async_client.post(
        "/api/v1/printers/",
        json={
            **body,
            "secret_actions": {"external_camera_url": "replace", "external_camera_snapshot_url": "replace"},
        },
    )
    assert created.status_code == 200, created.text
    assert "CANARY" not in created.text and created.json()["external_camera_url_configured"]
    row = await db_session.get(Printer, created.json()["id"])
    assert row.external_camera_url == STREAM and row.external_camera_snapshot_url == SNAPSHOT


async def test_missing_anchor_blocks_camera_create_before_mqtt_probe(async_client, db_session, monkeypatch):
    from backend.app.api.routes import printers

    await db_session.execute(delete(Settings).where(Settings.key == "_credential_config_guard"))
    await db_session.commit()
    probe = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(printers.printer_manager, "test_connection", probe)
    response = await async_client.post(
        "/api/v1/printers/",
        json={
            "name": "No anchor",
            "serial_number": "CAMERA_NO_ANCHOR",
            "ip_address": "192.0.2.15",
            "access_code": "12345678",
            "external_camera_url": STREAM,
        },
    )
    assert response.status_code == 503 and "CANARY" not in response.text
    probe.assert_not_awaited()
