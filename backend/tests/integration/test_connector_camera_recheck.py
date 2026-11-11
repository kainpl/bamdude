"""OQ2: live HTTP header streams stop delivery on changed authority without stopping peers."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import camera_fanout, camera_key_access
from backend.app.services.connector_runtime import connector_runtime
from backend.app.services.connector_submissions import STORAGE_EPOCH_KEY, server_identity

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
JPEG = b"\xff\xd8oq2-frame\xff\xd9"
CHUNK = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    connector_runtime._reset_for_tests()
    monkeypatch.setattr(camera_key_access, "RECHECK_SECONDS", 0.50)
    monkeypatch.setattr(camera_key_access, "CHECK_TIMEOUT_SECONDS", 0.50)
    monkeypatch.setattr(camera_fanout, "_DISCONNECT_POLL_SECONDS", 0.025)
    yield
    await camera_fanout.shutdown_all_broadcasters()
    # Timed-out checks remain owned until actual completion; no abandoned SQL at fixture teardown.
    if connector_runtime._requests:
        await asyncio.wait_for(asyncio.gather(*connector_runtime._requests, return_exceptions=True), 3)
    connector_runtime._reset_for_tests()


async def principal(db):
    await server_identity(db)
    group = Group(name=f"camera-{uuid.uuid4().hex}", permissions=["camera:view"])
    owner = User(username=uuid.uuid4().hex, password_hash="unused", is_active=True, role="user", groups=[group])
    db.add(owner)
    await db.flush()
    raw, hashed, prefix = generate_api_key()
    key = APIKey(name="camera", key_hash=hashed, key_prefix=prefix, user_id=owner.id, can_read_status=True)
    db.add(key)
    await db.commit()
    return raw, key, owner, group


@asynccontextmanager
async def streaming(app, printer_id, raw, header):
    first = asyncio.Event()
    bodies = []

    async def observed(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.body" and message.get("body"):
                bodies.append((time.monotonic(), message["body"]))
                first.set()
            await send(message)

        await app(scope, receive, capture)

    headers = {"X-API-Key": raw} if header == "key" else {"Authorization": f"Bearer {raw}"}
    async with AsyncClient(transport=ASGITransport(app=observed), base_url="http://test") as client:
        task = asyncio.create_task(client.get(f"/api/v1/printers/{printer_id}/camera/stream", headers=headers))
        try:
            waiting = asyncio.create_task(first.wait())
            try:
                done, _ = await asyncio.wait([waiting, task], timeout=5, return_when=asyncio.FIRST_COMPLETED)
                assert done, "stream did not start within 5 seconds"
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            if task.done():
                response = await task
                assert response.status_code == 200, response.text
            assert bodies, "stream closed before its first frame"
            yield task, bodies
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def runtime(monkeypatch, stalled=False):
    class FakeRuntime:
        async def stream_builtin(self, *, on_frame, disconnect_event, **kwargs):
            on_frame(JPEG)
            yield CHUNK
            if stalled:
                await disconnect_event.wait()
            else:
                while not disconnect_event.is_set():
                    await asyncio.sleep(0.01)
                    on_frame(JPEG)
                    yield CHUNK

        stream_external = stream_builtin

    value = FakeRuntime()
    monkeypatch.setattr("backend.app.services.camera_runtime.get_camera_runtime", lambda: value)
    return value


CHANGES = [
    "disable",
    "delete",
    "rekey",
    "recreate",
    "scope",
    "printers",
    "owner",
    "owner_inactive",
    "owner_deleted",
    "permissions",
    "printer_replaced",
    "epoch",
]


async def change_authority(db, key, owner, group, printer, change):
    if change == "delete":
        await db.execute(delete(APIKey).where(APIKey.id == key.id))
    elif change == "recreate":
        old_id = key.id
        await db.execute(delete(APIKey).where(APIKey.id == old_id))
        await db.execute(
            APIKey.__table__.insert().values(
                id=old_id,
                name="replacement",
                key_hash=key.key_hash,
                key_prefix=key.key_prefix,
                user_id=owner.id,
                enabled=True,
                can_read_status=True,
            )
        )
    elif change in {"disable", "rekey", "scope", "printers", "owner", "expire"}:
        values = {
            "disable": {"enabled": False},
            "rekey": {"key_hash": "replacement"},
            "scope": {"can_read_status": False},
            "printers": {"printer_ids": []},
            "owner": {"user_id": None},
            "expire": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        }
        await db.execute(update(APIKey).where(APIKey.id == key.id).values(**values[change]))
    elif change == "owner_inactive":
        await db.execute(update(User).where(User.id == owner.id).values(is_active=False))
    elif change == "owner_deleted":
        await db.execute(delete(User).where(User.id == owner.id))
    elif change == "permissions":
        await db.execute(update(Group).where(Group.id == group.id).values(permissions=[]))
    elif change == "printer_replaced":
        await db.execute(update(Printer).where(Printer.id == printer.id).values(connector_identity=str(uuid.uuid4())))
    elif change == "epoch":
        await db.execute(update(Settings).where(Settings.key == STORAGE_EPOCH_KEY).values(value=str(uuid.uuid4())))
    await db.commit()


@pytest.mark.parametrize("header", ["key", "bearer"])
@pytest.mark.parametrize("external,stalled", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("change", CHANGES + ["expire"])
async def test_http_delivery_stops_on_current_authority_change(
    async_client, db_session, printer_factory, monkeypatch, header, external, stalled, change
):
    printer = await printer_factory(
        external_camera_enabled=external, external_camera_url="http://camera.local/video", external_camera_type="mjpeg"
    )
    raw, key, owner, group = await principal(db_session)
    runtime(monkeypatch, stalled)
    async with streaming(async_client._transport.app, printer.id, raw, header) as (task, bodies):
        changed = time.monotonic()
        await change_authority(db_session, key, owner, group, printer, change)
        response = await asyncio.wait_for(asyncio.shield(task), 2)
        assert response.status_code == 200
        assert time.monotonic() - changed < 2
        assert all(at - changed < 2 for at, _ in bodies)
        if stalled:
            assert len(bodies) == 1


@pytest.mark.parametrize("header", ["key", "bearer"])
@pytest.mark.parametrize("stalled", [False, True])
async def test_real_default_bound_without_shortened_recheck(
    async_client, db_session, printer_factory, monkeypatch, header, stalled
):
    monkeypatch.setattr(camera_key_access, "RECHECK_SECONDS", 2.0)
    monkeypatch.setattr(camera_key_access, "CHECK_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(camera_fanout, "_DISCONNECT_POLL_SECONDS", 1.0)
    printer = await printer_factory()
    raw, key, owner, group = await principal(db_session)
    runtime(monkeypatch, stalled)
    async with streaming(async_client._transport.app, printer.id, raw, header) as (task, bodies):
        changed = time.monotonic()
        await change_authority(db_session, key, owner, group, printer, "disable")
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert all(at - changed < 5 for at, _ in bodies)


async def test_revoke_one_viewer_preserves_shared_upstream(async_client, db_session, printer_factory, monkeypatch):
    printer = await printer_factory()
    raw, key, owner, group = await principal(db_session)
    peer, *_ = await principal(db_session)
    runtime(monkeypatch)
    async with (
        streaming(async_client._transport.app, printer.id, raw, "key") as (first, _),
        streaming(async_client._transport.app, printer.id, peer, "bearer") as (second, bodies),
    ):
        bc = camera_fanout._broadcasters[f"printer-{printer.id}"]
        assert bc.subscriber_count == 2
        await change_authority(db_session, key, owner, group, printer, "disable")
        await asyncio.wait_for(asyncio.shield(first), 2)
        count = len(bodies)
        await asyncio.sleep(0.05)
        assert len(bodies) > count and not second.done()
        assert camera_fanout._broadcasters[f"printer-{printer.id}"] is bc
        assert bc.subscriber_count == 1 and not bc.stopped


async def test_check_timeout_closes_delivery_but_restore_still_owns_sql(
    async_client, db_session, printer_factory, monkeypatch
):
    printer = await printer_factory()
    raw, *_ = await principal(db_session)
    runtime(monkeypatch)
    release = asyncio.Event()
    original = camera_key_access.CameraKeyStreamGuard._allowed
    calls = 0

    async def blocked(self):
        nonlocal calls
        calls += 1
        if calls > 1:
            await release.wait()
        return await original(self)

    monkeypatch.setattr(camera_key_access.CameraKeyStreamGuard, "_allowed", blocked)
    try:
        async with streaming(async_client._transport.app, printer.id, raw, "key") as (task, bodies):
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert connector_runtime._requests
            count = len(bodies)
            release.set()
            await asyncio.sleep(0.05)
            assert not connector_runtime._requests and len(bodies) == count
    finally:
        release.set()


@pytest.mark.parametrize("stalled", [False, True])
async def test_database_failure_is_fail_closed_without_raw_error(
    async_client, db_session, printer_factory, monkeypatch, stalled, caplog
):
    printer = await printer_factory()
    raw, *_ = await principal(db_session)
    runtime(monkeypatch, stalled)
    async with streaming(async_client._transport.app, printer.id, raw, "key") as (task, _):
        monkeypatch.setattr(
            camera_key_access.CameraKeyStreamGuard,
            "_allowed",
            AsyncMock(side_effect=RuntimeError("private-database-detail")),
        )
        await asyncio.wait_for(asyncio.shield(task), 2)
    assert "private-database-detail" not in caplog.text


async def test_revoke_after_handshake_before_first_frame_delivers_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    printer = await printer_factory()
    raw, key, owner, group = await principal(db_session)
    runtime(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = camera_key_access.CameraKeyStreamGuard._allowed

    async def delayed(self):
        entered.set()
        await release.wait()
        return await original(self)

    monkeypatch.setattr(camera_key_access.CameraKeyStreamGuard, "_allowed", delayed)
    async with AsyncClient(transport=ASGITransport(app=async_client._transport.app), base_url="http://test") as client:
        task = asyncio.create_task(
            client.get(f"/api/v1/printers/{printer.id}/camera/stream", headers={"X-API-Key": raw})
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await change_authority(db_session, key, owner, group, printer, "disable")
            release.set()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 200 and not response.content
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_initial_namespace_read_remains_restore_owned_after_request_cancel(
    async_client, db_session, printer_factory, monkeypatch
):
    from backend.app.services import connector_submissions

    printer = await printer_factory()
    raw, *_ = await principal(db_session)
    entered, release = asyncio.Event(), asyncio.Event()
    original = connector_submissions.read_server_identity

    async def blocked(db):
        entered.set()
        await release.wait()
        return await original(db)

    monkeypatch.setattr(connector_submissions, "read_server_identity", blocked)
    async with AsyncClient(transport=ASGITransport(app=async_client._transport.app), base_url="http://test") as client:
        task = asyncio.create_task(
            client.get(f"/api/v1/printers/{printer.id}/camera/stream", headers={"X-API-Key": raw})
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert len(connector_runtime._requests) == 1
            assert not await connector_runtime._drain(0)
            release.set()
            assert await connector_runtime._drain(2)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
