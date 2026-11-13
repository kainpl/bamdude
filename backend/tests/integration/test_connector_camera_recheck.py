"""OQ2: live HTTP header streams stop delivery on changed authority without stopping peers."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, update

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import camera_fanout, live_authority
from backend.app.services.connector_runtime import connector_runtime
from backend.app.services.connector_submissions import STORAGE_EPOCH_KEY, server_identity
from backend.tests.auth_helpers import bind_test_key

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
JPEG = b"\xff\xd8oq2-frame\xff\xd9"
CHUNK = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"
logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
async def clean(monkeypatch, tmp_path):
    from backend.tests import conftest as shared_fixtures

    # Rechecks and authority mutations intentionally use concurrent sessions.
    # A cancelled aiosqlite read can invalidate StaticPool's one connection,
    # destroying an in-memory database beneath the mutation. Use the regular
    # shared bootstrap with a disposable file, so replacement connections keep
    # the schema and sessions have independent transaction/close ownership.
    monkeypatch.setattr(shared_fixtures, "TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'camera-recheck.db'}")
    connector_runtime._reset_for_tests()
    monkeypatch.setattr(live_authority, "RECHECK_SECONDS", 0.50)
    monkeypatch.setattr(live_authority, "CHECK_SECONDS", 1.0)
    # A healthy proof must outlive an allowed read plus its refresh tick.
    # Keep the production proof window; mutation/default delivery bounds below remain unchanged.
    monkeypatch.setattr(live_authority, "PROOF_SECONDS", 3.0)
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
    key = await bind_test_key(
        db,
        APIKey(name="camera", key_hash=hashed, key_prefix=prefix, user_id=owner.id, can_read_status=True),
        owner=owner,
    )
    db.add(key)
    await db.commit()
    await db.close()
    return raw, key, owner, group


def start_stream(app, printer_id, raw, header):
    """Observe the actual server-facing ASGI boundary, including truncation.

    Retirement deliberately ends an already-started response without a final
    body, which httpx's buffered ASGITransport treats as a protocol assertion.
    This receiver records status and every body send start without requiring
    a terminal chunk that a revoked viewer must never receive.
    """
    first = asyncio.Event()
    bodies = []
    response = SimpleNamespace(status_code=None, content=b"")
    path = f"/api/v1/printers/{printer_id}/camera/stream"
    headers = [(b"x-api-key", raw.encode())] if header == "key" else [(b"authorization", f"Bearer {raw}".encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    requested = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            response.status_code = message["status"]
        elif message["type"] == "http.response.body" and message.get("body"):
            bodies.append((time.monotonic(), message["body"]))
            if response.status_code == 200 and JPEG in message["body"]:
                first.set()

    async def run():
        await app(scope, receive, send)
        response.content = b"".join(body for _, body in bodies)
        return response

    return asyncio.create_task(run()), bodies, first


@asynccontextmanager
async def streaming(app, printer_id, raw, header):
    # The oracle starts at a successful private-frame admission, before its
    # caller mutates authority. A bounded retry here tolerates an explicitly
    # retryable initial 503 under parallel-host load; never retry delivery or
    # a failure after the mutation has started.
    for attempt in range(3):
        task, bodies, first = start_stream(app, printer_id, raw, header)
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
                if response.status_code == 503 and not first.is_set() and attempt < 2:
                    assert not any(JPEG in body for _, body in bodies)
                    logger.info("Camera test pre-mutation setup retry: status=503 attempt=%s", attempt + 1)
                    await asyncio.sleep(0.05)
                    continue
                assert response.status_code == 200, response.content
            assert first.is_set() and bodies, "stream closed before its first private frame"
            yield task, bodies
            return
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
                # Windows asyncio can promote sub-resolution timers immediately.
                # Pace the fake without changing any authority deadline below.
                interval = max(0.01, 2 * time.get_clock_info("monotonic").resolution)
                while not disconnect_event.is_set():
                    await asyncio.sleep(interval)
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
    await db.close()


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
    monkeypatch.setattr(live_authority, "RECHECK_SECONDS", 1.0)
    monkeypatch.setattr(live_authority, "CHECK_SECONDS", 1.0)
    monkeypatch.setattr(live_authority, "PROOF_SECONDS", 3.0)
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
    entered, deadline_cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    coordinator = live_authority.live_authority
    original = coordinator._read

    async def blocked(batch):
        result = await original(batch)
        if any(handle.admitted for handle in batch):
            entered.set()
            # Model an actual driver/continuation that has not completed even
            # when the check timeout cancels its waiter. The real read occurs
            # first; ownership must persist through this final continuation.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    deadline_cancelled.set()  # the real CHECK_SECONDS timeout cancelled this actual check
                    continue
        return result

    monkeypatch.setattr(coordinator, "_read", blocked)
    try:
        async with streaming(async_client._transport.app, printer.id, raw, "key") as (task, bodies):
            await asyncio.wait_for(entered.wait(), 2)
            actual = coordinator._check
            admitted = [handle for handle in coordinator.handles if handle.admitted and not handle.retired]
            assert len(admitted) == 1
            handle = admitted[0]
            await asyncio.wait_for(deadline_cancelled.wait(), 2)
            assert actual in coordinator.tasks and not actual.done()
            assert handle.deliverable()  # healthy setup did not expire before the injected failure
            # Expired-proof fault only after actual timeout cancellation is
            # observed. The live gate must stop delivery while real work stays
            # charged, without shrinking every healthy viewer's proof window.
            handle.proof_until = time.monotonic() - 0.01
            coordinator._wake.set()
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert actual in coordinator.tasks and not actual.done()
            assert not await coordinator.drain(0)
            count = len(bodies)
            release.set()
            assert await coordinator.drain(2)
            assert actual.done() and actual not in coordinator.tasks
            assert len(bodies) == count
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
            live_authority.live_authority,
            "_read",
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
    original = live_authority.live_authority._read

    async def delayed(batch):
        if batch:
            # The header was accepted, but no live proof has been published.
            entered.set()
            await release.wait()
        return await original(batch)

    monkeypatch.setattr(live_authority.live_authority, "_read", delayed)
    task, bodies, _ = start_stream(async_client._transport.app, printer.id, raw, "key")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await change_authority(db_session, key, owner, group, printer, "disable")
        release.set()
        response = await asyncio.wait_for(task, 2)
        # D admits before starting headers: a retired pending proof is 503,
        # never an empty successful 200. No private media send may start.
        assert response.status_code == 503
        assert not any(JPEG in body for _, body in bodies)
        assert not camera_fanout.get_subscriber_count(f"printer-{printer.id}")
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
    task, _, _ = start_stream(async_client._transport.app, printer.id, raw, "key")
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
