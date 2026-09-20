"""The monitor's fleet projection, ownership and independent TV grant."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from backend.app.core.auth import create_access_token
from backend.app.models.library import LibraryFile
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.monitor_snapshot import MonitorAccess, build_snapshot
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_api_key_owner_authority import _key, _user
from backend.tests.integration.test_long_lived_tokens_api import _create_user, _login
from backend.tests.unit.services.test_product_composition import counting_statements


@pytest.fixture
def telemetry(monkeypatch):
    from backend.app.services import monitor_snapshot

    state = PrinterState(
        connected=True, state="RUNNING", remaining_time=5, progress=80, subtask_name="SECRET current job", stg_cur=0
    )
    received = datetime.now(timezone.utc).timestamp()
    peek = Mock(return_value=(state, received, False))
    monkeypatch.setattr(printer_manager, "peek_status", peek)
    # Any accidental read through get_status would reconnect equipment.
    monkeypatch.setattr(printer_manager, "get_status", Mock(side_effect=AssertionError("mutating status read")))
    monkeypatch.setattr(printer_manager, "is_awaiting_plate_clear", lambda _: False)
    monkeypatch.setattr(
        monitor_snapshot.background_dispatch,
        "get_state",
        AsyncMock(
            return_value={
                "active_jobs": [],
                "dispatched_jobs": [],
            }
        ),
    )
    return state, peek


async def _token(client, scope="monitor"):
    response = await client.post(
        "/api/v1/auth/tokens",
        json={
            "name": "Workshop TV",
            "scope": scope,
            "expires_in_days": 90,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _queued(db, printer, *, owner=None):
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing", is_paused=True)
    source = LibraryFile(filename="SECRET next job.3mf", file_path="SECRET path", file_size=1, file_type="gcode")
    db.add_all([queue, source])
    await db.flush()
    item = PrintQueueItem(
        queue_id=queue.id, library_file_id=source.id, status="pending", position=1, created_by_id=owner
    )
    db.add(item)
    await db.commit()
    return item


async def test_snapshot_keeps_independent_queue_pause_and_minutes_to_seconds(
    async_client, printer_factory, db_session, telemetry
):
    printer = await printer_factory()
    await _queued(db_session, printer)
    response = await async_client.get("/api/v1/monitor/snapshot?view=queues")
    assert response.status_code == 200, response.text
    body = response.json()
    tile = body["printers"][0]
    assert tile["state"] == "RUNNING" and tile["remaining_seconds"] == 300
    assert tile["queue"]["is_paused"] and tile["queue"]["pending_count"] == 1
    assert tile["queue"]["next_job"]["name"] == "SECRET next job.3mf"
    assert body["generated_at"].endswith("Z") and tile["status_received_at"].endswith("Z")
    assert response.headers["cache-control"] == "no-store"


async def test_tv_redacts_nested_names_and_uses_same_forecast(async_client, printer_factory, db_session, telemetry):
    printer = await printer_factory(
        serial_number="SECRET serial", ip_address="SECRET address", access_code="SECRET code"
    )
    await _queued(db_session, printer)
    token = await _token(async_client)
    headers = {"Authorization": f"Bearer {token['token']}"}
    for view in ("printers", "queues"):
        response = await async_client.get(f"/api/v1/monitor/kiosk/snapshot?view={view}", headers=headers)
        assert response.status_code == 200, response.text
        assert "SECRET" not in response.text
        body = response.json()
        assert body["capabilities"] == {
            "queues": True,
            "forecast": True,
            "job_details": False,
            "open_printer": False,
            "open_queue": False,
        }
        assert body["printers"][0]["current_job"] == {"visibility": "restricted"}
        if view == "queues":
            assert body["printers"][0]["queue"]["next_job"] == {"visibility": "restricted"}
    public_forecast = await async_client.get("/api/v1/monitor/kiosk/forecast", headers=headers)
    private_forecast = await async_client.get("/api/v1/queue/forecast")
    assert public_forecast.status_code == 200
    assert public_forecast.json()["free_seconds"] == private_forecast.json()["free_seconds"]
    assert "SECRET" not in public_forecast.text
    assert public_forecast.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("scope", ["camera_stream", "camwall", "overlay"])
async def test_camera_scopes_cannot_read_monitor(async_client, scope):
    token = await _token(async_client, scope)
    for endpoint in ("snapshot", "forecast"):
        response = await async_client.get(
            f"/api/v1/monitor/kiosk/{endpoint}", headers={"Authorization": f"Bearer {token['token']}"}
        )
        assert response.status_code == 401


async def test_monitor_scope_cannot_read_ordinary_or_camera_endpoints(async_client, printer_factory):
    printer = await printer_factory()
    token = await _token(async_client)
    headers = {"Authorization": f"Bearer {token['token']}"}
    for path in ("/monitor/snapshot", "/printers/", "/queue/forecast"):
        response = await async_client.get(f"/api/v1{path}", headers=headers)
        assert response.status_code == 401
    for path in ("/camwall/printers", f"/printers/{printer.id}/overlay-status"):
        response = await async_client.get(f"/api/v1{path}", params={"token": token["token"]})
        assert response.status_code == 401
    response = await async_client.post(f"/api/v1/printers/{printer.id}/pause", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("termination", ["revoke", "expire", "deactivate", "permissions"])
async def test_tv_grant_is_rechecked_on_every_request(async_client, db_session, termination):
    token = await _token(async_client)
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert (await async_client.get("/api/v1/monitor/kiosk/snapshot", headers=headers)).status_code == 200
    if termination == "revoke":
        assert (await async_client.delete(f"/api/v1/auth/tokens/{token['id']}")).status_code == 204
    elif termination == "expire":
        record = await db_session.get(LongLivedToken, token["id"])
        record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
    else:
        owner = await db_session.get(User, token["user_id"])
        if termination == "deactivate":
            owner.is_active = False
        else:
            await db_session.refresh(owner, ["groups"])
            owner.role = "user"
            owner.groups = []
        await db_session.commit()
    assert (await async_client.get("/api/v1/monitor/kiosk/snapshot", headers=headers)).status_code == 401


async def test_camera_viewer_cannot_mint_monitor_and_old_camera_scope_still_works(async_client):
    await _create_user(async_client, "monitor_viewer")
    token = await _login(async_client, "monitor_viewer")
    headers = {"Authorization": f"Bearer {token}"}
    for scope, expected in (("monitor", 403), ("camera_stream", 201)):
        response = await async_client.post(
            "/api/v1/auth/tokens", headers=headers, json={"scope": scope, "name": "screen", "expires_in_days": 30}
        )
        assert response.status_code == expected, response.text


async def test_projection_never_substitutes_an_owned_row_for_hidden_head(db_session, printer_factory, telemetry):
    printer = await printer_factory()
    head = await _queued(db_session, printer)
    owner = User(username="monitor_owner", password_hash="test-only", role="user")
    db_session.add(owner)
    await db_session.flush()
    owned = PrintQueueItem(
        queue_id=printer.id, library_file_id=head.library_file_id, status="pending", position=2, created_by_id=owner.id
    )
    db_session.add(owned)
    await db_session.commit()
    result = await build_snapshot(db_session, "queues", MonitorAccess(queue_read=True, read_own=True, user_id=owner.id))
    assert result.printers[0].queue.next_job.model_dump() == {"visibility": "restricted"}
    assert result.printers[0].queue.pending_count == 2
    assert result.printers[0].queue.waiting is None


async def test_fleet_query_count_is_bounded_and_archived_is_distinct_from_maintenance(
    db_session, test_engine, printer_factory, telemetry
):
    parked = await printer_factory(name="parked", is_active=False)
    await _queued(db_session, parked)
    await printer_factory(name="retired", archived=True)
    access = MonitorAccess(queue_read=True, read_all=True)
    with counting_statements(test_engine) as one:
        first = await build_snapshot(db_session, "printers", access)
    assert [p.name for p in first.printers] == ["parked"]
    for i in range(49):
        printer = await printer_factory(name=f"Printer {i}", serial_number=f"MONITOR-{i}")
        await _queued(db_session, printer)
    with counting_statements(test_engine) as many:
        fleet = await build_snapshot(db_session, "queues", access)
    assert len(fleet.printers) == 50
    assert all(p.queue.pending_count == 1 and p.queue.next_job.name for p in fleet.printers)
    assert len(many) == len(one)
    assert len(many) <= 8


async def test_http_success_does_not_refresh_stale_mqtt(async_client, printer_factory, telemetry):
    await printer_factory()
    state, peek = telemetry
    timestamp = datetime.now(timezone.utc).timestamp() - 120
    peek.return_value = (state, timestamp, True)
    response = await async_client.get("/api/v1/monitor/snapshot")
    tile = response.json()["printers"][0]
    assert tile["source_stale"] and not tile["connected"] and tile["last_known_work_active"]
    assert datetime.fromisoformat(tile["status_received_at"]).timestamp() == timestamp


async def test_no_snapshot_is_not_fabricated_idle(async_client, printer_factory, telemetry):
    await printer_factory()
    telemetry[1].return_value = (None, None, False)
    response = await async_client.get("/api/v1/monitor/snapshot")
    tile = response.json()["printers"][0]
    assert tile["state"] is None and tile["remaining_seconds"] is None and tile["status_received_at"] is None


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
async def test_ended_work_does_not_keep_a_leftover_job_eta(async_client, printer_factory, telemetry, state):
    await printer_factory()
    telemetry[0].state = state
    tile = (await async_client.get("/api/v1/monitor/snapshot")).json()["printers"][0]
    assert tile["remaining_seconds"] is None and tile["current_job"] is None


async def test_reader_without_queue_permission_gets_no_job_or_queue_details(
    async_client, db_session, printer_factory, telemetry
):
    printer = await printer_factory()
    await _queued(db_session, printer)
    reader = await _user(db_session, "monitor_printers_only", ["printers:read"])
    jwt = create_access_token(data={"sub": reader.username})
    headers = {"Authorization": f"Bearer {jwt}"}
    response = await async_client.get("/api/v1/monitor/snapshot", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["printers"][0]["queue"] is None
    assert body["printers"][0]["current_job"] == {"visibility": "restricted"}
    assert not body["capabilities"]["queues"] and not body["capabilities"]["forecast"]
    assert "SECRET" not in response.text
    assert (await async_client.get("/api/v1/monitor/snapshot?view=queues", headers=headers)).status_code == 403


async def test_api_key_scope_still_obeys_its_owner_and_cannot_list_tokens(
    async_client, db_session, printer_factory, telemetry
):
    await printer_factory()
    reader = await _user(db_session, "monitor_key_owner", ["printers:read", "api_keys:read"])
    raw, _ = await _key(db_session, owner=reader, can_read_status=True, can_queue=True)
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await async_client.get("/api/v1/monitor/snapshot", headers=headers)).status_code == 200
    assert (await async_client.get("/api/v1/monitor/snapshot?view=queues", headers=headers)).status_code == 403
    for path in ("/api/v1/auth/tokens", "/api/v1/auth/tokens/all"):
        assert (await async_client.get(path, headers=headers)).status_code == 403


async def test_monitor_creator_can_manage_own_token_without_camera_permission(async_client, db_session):
    reader = await _user(
        db_session,
        "monitor_custom",
        ["printers:read", "queue:read", "api_keys:create", "api_keys:read", "api_keys:delete"],
    )
    headers = {"Authorization": f"Bearer {create_access_token(data={'sub': reader.username})}"}
    response = await async_client.post(
        "/api/v1/auth/tokens", headers=headers, json={"scope": "monitor", "name": "TV", "expires_in_days": 90}
    )
    assert response.status_code == 201, response.text
    token = response.json()
    listing = await async_client.get("/api/v1/auth/tokens", headers=headers)
    assert listing.status_code == 200 and [r["id"] for r in listing.json()] == [token["id"]]
    assert "bblt_" not in listing.text
    camera = await async_client.post(
        "/api/v1/auth/tokens", headers=headers, json={"scope": "camera_stream", "name": "Camera", "expires_in_days": 90}
    )
    assert camera.status_code == 403
    assert (await async_client.delete(f"/api/v1/auth/tokens/{token['id']}", headers=headers)).status_code == 204
