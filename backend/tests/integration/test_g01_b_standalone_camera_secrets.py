"""BA4: independent camera sources are opaque, editable and worker-only."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.core.auth import create_access_token
from backend.app.models.camera import Camera
from backend.app.models.group import Group
from backend.app.models.user import User

URL = "rtsp://user:BA4_PASSWORD@192.0.2.71/live?token=BA4_STREAM"
SNAP = "http://user:BA4_SNAPSHOT@192.0.2.72/frame?token=BA4_QUERY"


async def seed(db):
    row = Camera(name="Private shelf", camera_type="rtsp", url=URL, snapshot_url=SNAP, enabled=True)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
async def test_read_never_returns_sources(role, async_client, db_session):
    await seed(db_session)
    headers = {}
    if role != "admin":
        group = Group(
            name="Camera " + role,
            permissions=["settings:read", "camera:view"] + (["settings:update"] if role == "editor" else []),
        )
        user = User(username="standalone_" + role, role="user", is_active=True, groups=[group])
        db_session.add(user)
        await db_session.commit()
        headers = {"Authorization": "Bearer " + create_access_token({"sub": user.username})}
    response = await async_client.get("/api/v1/cameras/", headers=headers)
    assert response.status_code == 200 and "BA4_" not in response.text
    row = response.json()[0]
    assert "url" not in row and "snapshot_url" not in row
    assert row["url_configured"] and row["snapshot_url_configured"] and row["source_host"] == "192.0.2.71"


async def test_saved_draft_authority_no_transaction_and_no_persistence(async_client, db_session, monkeypatch):
    camera = await seed(db_session)
    camera_id = camera.id
    from backend.app.api.routes import cameras
    from backend.app.services import camera_runtime

    calls = []

    async def probe(url, camera_type):
        calls.append((url, camera_type))
        return {"success": False, "error": url}

    monkeypatch.setattr(camera_runtime, "test_external_connection", probe)
    group = Group(name="Standalone viewer", permissions=["settings:read", "camera:view"])
    user = User(username="standalone_viewer", role="user", is_active=True, groups=[group])
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": "Bearer " + create_access_token({"sub": user.username})}
    response = await async_client.post(f"/api/v1/cameras/{camera_id}/test", headers=headers)
    assert response.status_code == 403 and not calls
    response = await async_client.post(
        "/api/v1/cameras/test", headers=headers, json={"url": "http://192.0.2.73/new", "camera_type": "snapshot"}
    )
    assert response.status_code == 200 and "192.0.2.73" not in response.text
    calls.clear()

    async def db_override():
        yield db_session

    # Resolve the actual route dependency with this known session to assert TX release.
    from backend.app.core.database import get_db
    from backend.app.main import app

    old = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = db_override

    async def checked_probe(url, camera_type):
        assert not db_session.in_transaction()
        calls.append((url, camera_type))
        return {"success": False, "error": url}

    monkeypatch.setattr(camera_runtime, "test_external_connection", checked_probe)
    try:
        response = await async_client.post(f"/api/v1/cameras/{camera_id}/test", json={"source": "snapshot"})
        assert response.status_code == 200 and "BA4_" not in response.text and calls == [(SNAP, "snapshot")]
        response = await async_client.post(
            f"/api/v1/cameras/{camera_id}/test",
            json={"source": "snapshot", "enabled": False, "secret_actions": {"url": "clear", "snapshot_url": "keep"}},
        )
        assert response.status_code == 200 and calls[-1] == (SNAP, "snapshot")
        replacement = "http://192.0.2.74/BA4_DRAFT"
        response = await async_client.post(
            f"/api/v1/cameras/{camera_id}/test", json={"url": replacement, "secret_actions": {"url": "replace"}}
        )
        assert response.status_code == 200 and "BA4_" not in response.text
        assert calls[-1] == (replacement, "rtsp")
    finally:
        if old is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = old
    await db_session.refresh(camera)
    assert camera.url == URL and camera.snapshot_url == SNAP


async def test_patch_actions_and_missing_anchor(async_client, db_session, monkeypatch):
    camera = await seed(db_session)
    door = f"/api/v1/cameras/{camera.id}"
    response = await async_client.patch(
        door, json={"name": "Renamed", "secret_actions": {"url": "keep", "snapshot_url": "keep"}}
    )
    assert response.status_code == 200 and "BA4_" not in response.text
    for body in (
        {"secret_actions": {"name": "keep"}},
        {"secret_actions": {"url": "keep"}, "url": URL},
        {"url": "", "secret_actions": {"url": "replace"}},
    ):
        response = await async_client.patch(door, json=body)
        assert response.status_code == 422 and "BA4_" not in response.text
    assert (await async_client.patch(door, json={"camera_type": "mjpeg"})).status_code == 400
    assert (await async_client.patch(door, json={"secret_actions": {"url": "clear"}})).status_code == 400
    response = await async_client.patch(
        door, json={"enabled": False, "secret_actions": {"url": "clear", "snapshot_url": "clear"}}
    )
    assert (
        response.status_code == 200
        and not response.json()["url_configured"]
        and not response.json()["snapshot_url_configured"]
    )
    assert (await async_client.patch(door, json={"enabled": True})).status_code == 400
    replacement = "http://192.0.2.75/BA4_REPLACED"
    response = await async_client.patch(
        door, json={"url": replacement, "camera_type": "mjpeg", "enabled": True, "secret_actions": {"url": "replace"}}
    )
    assert response.status_code == 200 and "BA4_" not in response.text
    await db_session.refresh(camera)
    assert camera.url == replacement
    from sqlalchemy import delete

    from backend.app.core.credential_config import CONFIG_GUARD_KEY
    from backend.app.models.settings import Settings

    await db_session.execute(delete(Settings).where(Settings.key == CONFIG_GUARD_KEY))
    await db_session.commit()
    probe = AsyncMock()
    monkeypatch.setattr("backend.app.services.camera_runtime.test_external_connection", probe)
    assert (await async_client.post(f"{door}/test")).status_code == 503
    probe.assert_not_awaited()


async def test_worker_exception_and_null_fields_are_safe(async_client, db_session, monkeypatch):
    camera = await seed(db_session)

    async def probe(*args):
        raise RuntimeError(URL)

    monkeypatch.setattr("backend.app.services.camera_runtime.test_external_connection", probe)
    response = await async_client.post(f"/api/v1/cameras/{camera.id}/test")
    assert response.status_code == 200 and "BA4_" not in response.text
    for field in ("name", "url", "camera_type", "rotation", "enabled"):
        response = await async_client.patch(f"/api/v1/cameras/{camera.id}", json={field: None})
        assert response.status_code == 422
    await db_session.refresh(camera)
    assert camera.url == URL and camera.name == "Private shelf" and camera.enabled
