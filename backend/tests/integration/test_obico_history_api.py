"""Real JWT/key gates and the normal HTTP history/media/clear routes."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.core import database
from backend.app.models.obico_event import ObicoEvent
from backend.app.models.user import User
from backend.app.services.obico_history import ObicoHistory, filename
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user
from backend.tests.unit.services.test_obico_history import jpeg, save

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def api_history(async_client, printer_factory, tmp_path, monkeypatch):
    from backend.app.api.routes import obico
    from backend.app.services import obico_history as module

    printers = [await printer_factory(name=f"History {i}") for i in range(3)]
    history = ObicoHistory(tmp_path / "obico-detections", database.async_session)
    await history.start(enabled=True)
    monkeypatch.setattr(module, "obico_history", history)
    monkeypatch.setattr(obico, "obico_history", history)
    monkeypatch.setattr("backend.app.services.obico_detection.async_session", database.async_session)
    ids = [await save(history, printer=p.id) for p in printers]
    yield async_client, history, printers, ids
    await history.shutdown()


async def test_metadata_media_camera_gate_anonymous_and_forged_pair(api_history, db_session):
    client, history, printers, ids = api_history
    p = printers[0].id
    operator = await _user(db_session, "metadata", ["printers:read"])
    headers = await _jwt(operator.username)
    listed = await client.get("/api/v1/obico/history", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["saved_count"] == 3
    assert not {"raw_boxes", "boxes", "facts", "sha256", "width"}.intersection(listed.json()["items"][0])
    for suffix in ("", "/image"):
        assert (
            await client.get(f"/api/v1/obico/printers/{p}/events/{ids[0]}{suffix}", headers=headers)
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/obico/printers/{p}/events/{ids[0]}{suffix}", headers={"Authorization": ""})
        ).status_code == 401
    viewer = await _user(db_session, "viewer", ["printers:read", "camera:view"])
    headers = await _jwt(viewer.username)
    detail = await client.get(f"/api/v1/obico/printers/{p}/events/{ids[0]}", headers=headers)
    assert detail.status_code == 200 and detail.json()["raw_boxes"][0][2] == [320, 180, 100, 80]
    image = await client.get(f"/api/v1/obico/printers/{p}/events/{ids[0]}/image", headers=headers)
    assert image.status_code == 200 and image.content == jpeg()
    assert image.headers["cache-control"] == "private, no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert (
        await client.get(f"/api/v1/obico/printers/{printers[1].id}/events/{ids[0]}/image", headers=headers)
    ).status_code == 404
    assert (await client.delete("/api/v1/obico/history", headers=headers)).status_code == 403
    assert (await client.get(f"/api/v1/obico/cached-frame/{ids[0]}")).status_code == 404


@pytest.mark.parametrize("scope", [None, [], "last"])
async def test_keys_scope_before_paging_and_existing_clear_denylist(api_history, db_session, scope):
    client, history, printers, ids = api_history
    selected = [printers[-1].id] if scope == "last" else scope
    owner = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    raw = await _key(db_session, owner, can_read_status=True, printer_ids=selected)
    headers = {"Authorization": f"Bearer {raw}"}
    response = await client.get("/api/v1/obico/history?limit=1", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved_count"] == (3 if scope is None else (1 if scope == "last" else 0))
    if selected is not None:
        assert body["next_cursor"] is None
    if selected:
        assert body["items"][0]["printer_id"] == selected[0]
    assert (await client.delete("/api/v1/obico/history", headers=headers)).status_code == 403
    if selected is not None and not selected:
        assert body["items"] == [] and body["oldest_saved_at"] is None
    if scope == "last":
        allowed = await client.get(f"/api/v1/obico/printers/{printers[-1].id}/events/{ids[-1]}/image", headers=headers)
        assert allowed.status_code == 200
        denied = await client.get(f"/api/v1/obico/printers/{printers[0].id}/events/{ids[0]}/image", headers=headers)
        assert denied.status_code == 403


async def test_key_owner_ceiling_and_cursor_scope(api_history, db_session):
    client, history, printers, ids = api_history
    owner = await _user(db_session, "owner", ["printers:read"])
    raw = await _key(db_session, owner, can_read_status=True)
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await client.get("/api/v1/obico/history", headers=headers)).status_code == 200
    assert (
        await client.get(f"/api/v1/obico/printers/{printers[0].id}/events/{ids[0]}/image", headers=headers)
    ).status_code == 403
    first = (await client.get("/api/v1/obico/history?limit=1", headers=headers)).json()
    other = await _key(db_session, owner, can_read_status=True)
    assert (
        await client.get(
            "/api/v1/obico/history",
            params={"cursor": first["next_cursor"]},
            headers={"Authorization": f"Bearer {other}"},
        )
    ).status_code == 409


async def test_clear_and_actual_printer_delete_under_backup_pin(api_history, db_session):
    client, history, printers, ids = api_history
    async with history.pin_backup():
        deleted = await client.delete(f"/api/v1/printers/{printers[0].id}?delete_archives=false")
        assert deleted.status_code == 200, deleted.text
        assert (history.root / filename(ids[0])).exists()
        async with history.session() as db:
            assert await db.get(ObicoEvent, ids[0]) is None
        response = await client.delete("/api/v1/obico/history")
        assert response.status_code == 200 and response.json() == {"deleted": 2}
        assert (history.root / filename(ids[1])).exists()
    assert not list(history.root.iterdir())


async def test_slow_printer_delete_allows_other_recording_status_and_backup(api_history, monkeypatch):
    from backend.app.services import print_completion_tasks

    client, history, printers, ids = api_history
    inside, release = asyncio.Event(), asyncio.Event()

    async def stop(printer_id):
        inside.set()
        await release.wait()

    monkeypatch.setattr(print_completion_tasks, "stop_print_completion_tasks", stop)
    deleting = asyncio.create_task(client.delete(f"/api/v1/printers/{printers[0].id}?delete_archives=false"))
    try:
        await asyncio.wait_for(inside.wait(), 5)
        assert printers[0].id in history._blocked
        response = await asyncio.wait_for(client.get("/api/v1/obico/status"), 5)
        assert response.status_code == 200

        async def record_and_pin():
            async with history.pin_backup():
                for _ in range(12):
                    await save(history, printer=printers[1].id, verdict="failure")

        await asyncio.wait_for(record_and_pin(), 5)
        page = await asyncio.wait_for(history.page(scope=None), 5)
        assert page["total_count"] == 15
        assert not history.dropped.get("queue_full")
        assert not deleting.done()  # progress happened while deletion was paused
    finally:
        release.set()
        response = await asyncio.wait_for(deleting, 10)
    assert response.status_code == 200, response.text
    assert not (history.root / filename(ids[0])).exists()
    assert await history.quiesce(timeout=5)
    rejected = await client.delete(f"/api/v1/printers/{printers[1].id}?delete_archives=false")
    assert rejected.status_code == 503
    async with history.session() as db:
        assert await db.get(ObicoEvent, ids[1]) is not None


async def test_enriched_status_filters_recent_and_live_printers(api_history, db_session, monkeypatch):
    from backend.app.api.routes import obico

    client, history, printers, ids = api_history
    owner = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    raw = await _key(db_session, owner, can_read_status=True, printer_ids=[printers[-1].id])
    monkeypatch.setattr(
        obico.obico_detection_service,
        "_history",
        [
            {"id": i, "printer_id": p.id, "timestamp": "2026-11-03T00:00:00Z"}
            for p, i in zip(printers, ids, strict=True)
        ],
    )
    monkeypatch.setattr(
        obico.obico_detection_service,
        "get_per_printer",
        lambda: {p.id: {"class": "warning", "score": 0.3, "frame_count": 5, "error": None} for p in printers},
    )
    response = await client.get("/api/v1/obico/status", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200, response.text
    assert [event["id"] for event in response.json()["history"]] == [ids[-1]]
    assert response.json()["history"][0]["state"] == "ready"
    assert set(response.json()["per_printer"]) == {str(printers[-1].id)}
    assert response.json()["storage"] is None
