"""A printer camera has two doors, and neither falls back on the other (OrcaBamDude spec r2 §11, addendum C4).

- Headers: the ordinary ``camera:view`` gate -- the key's ``can_read_status``, its owner's ceiling and its printer
  list are decided before any camera work starts. An invalid header is refused even beside a valid token.
- No headers: the existing ``?token=`` for an ``<img>`` keeps working.
- ``X-BamDude-Printer-Identity`` that does not match is a 404 before the camera is touched.
- An API-key stream ends at its presented credential expiry; it cannot renew itself.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.core.auth import create_camera_stream_token, generate_api_key
from backend.app.core.auth_lifetime import utcnow
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.user import User
from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.connector_runtime import connector_runtime
from backend.tests.auth_helpers import bind_test_key, mint_test_authority
from backend.tests.unit.live_media_harness import request as make_request

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

JPEG = b"\xff\xd8\xff\xe0connector-frame"


@pytest.fixture(autouse=True)
def _clean():
    from backend.app.api.routes import camera

    connector_runtime._reset_for_tests()
    camera._snapshot_frames.clear()
    camera._snapshot_frame_times.clear()
    yield
    camera._snapshot_frames.clear()
    camera._snapshot_frame_times.clear()
    connector_runtime._reset_for_tests()


async def _key(db_session, *, owner=None, **flags) -> str:
    from backend.app.services.connector_submissions import server_identity

    await server_identity(db_session)  # Production connector startup initializes these before camera admission.
    raw, key_hash, prefix = generate_api_key()
    values = {"can_read_status": True, **flags}
    db_session.add(
        await bind_test_key(
            db_session,
            APIKey(
                name=f"cam-{prefix}",
                key_hash=key_hash,
                key_prefix=prefix,
                enabled=True,
                user_id=owner.id if owner else None,
                **values,
            ),
        )
    )
    await db_session.commit()
    return raw


def _headerless(async_client) -> AsyncClient:
    """The same app without the default JWT header -- what an ``<img>`` or the connector sends."""
    return AsyncClient(transport=ASGITransport(app=async_client._transport.app), base_url="http://test")


@pytest.fixture
def capture():
    with patch("backend.app.services.camera_runtime.capture", new_callable=AsyncMock) as mocked:
        mocked.return_value = CameraCaptureResult(JPEG, "fresh")
        yield mocked


async def test_an_api_key_reads_a_snapshot_without_a_token(async_client, db_session, printer_factory, capture):
    printer = await printer_factory()
    raw = await _key(db_session)

    async with _headerless(async_client) as client:
        response = await client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", headers={"X-API-Key": raw})

    assert response.status_code == 200 and response.content == JPEG


async def test_a_key_restricted_to_one_printer_never_opens_another(async_client, db_session, printer_factory, capture):
    allowed, other = await printer_factory(), await printer_factory()
    raw = await _key(db_session, printer_ids=[allowed.id])

    async with _headerless(async_client) as client:
        response = await client.get(f"/api/v1/printers/{other.id}/camera/snapshot", headers={"X-API-Key": raw})

    assert response.status_code == 403
    capture.assert_not_awaited()


async def test_an_invalid_header_does_not_fall_back_on_a_valid_token(async_client, printer_factory, capture):
    printer = await printer_factory()
    token = await create_camera_stream_token(authority=await mint_test_authority())

    async with _headerless(async_client) as client:
        response = await client.get(
            f"/api/v1/printers/{printer.id}/camera/snapshot",
            params={"token": token},
            headers={"X-API-Key": "bd_not-a-key"},
        )

    assert response.status_code == 401
    capture.assert_not_awaited()


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "Basic abc"}, {"Authorization": ""}, {"Authorization": "Bearer "}, {"X-API-Key": ""}],
)
async def test_malformed_or_empty_header_never_selects_the_query_token_door(
    async_client, printer_factory, capture, headers
):
    printer = await printer_factory()
    token = await create_camera_stream_token(authority=await mint_test_authority())
    async with _headerless(async_client) as client:
        response = await client.get(
            f"/api/v1/printers/{printer.id}/camera/snapshot", params={"token": token}, headers=headers
        )
    assert response.status_code == 401
    capture.assert_not_awaited()


async def test_explicit_invalid_key_does_not_fall_back_to_valid_jwt(async_client, printer_factory, capture):
    printer = await printer_factory()
    response = await async_client.get(
        f"/api/v1/printers/{printer.id}/camera/snapshot", headers={"X-API-Key": "bd_invalid"}
    )
    assert response.status_code == 401
    capture.assert_not_awaited()


async def test_a_key_without_status_scope_is_refused(async_client, db_session, printer_factory, capture):
    printer = await printer_factory()
    raw = await _key(db_session, can_read_status=False)

    async with _headerless(async_client) as client:
        response = await client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", headers={"X-API-Key": raw})

    assert response.status_code == 403
    capture.assert_not_awaited()


async def test_a_key_never_outranks_an_owner_without_camera_rights(async_client, db_session, printer_factory, capture):
    printer = await printer_factory()
    group = Group(name="no-camera", description="test", permissions=["printers:read"])
    db_session.add(group)
    await db_session.flush()
    owner = User(username="viewer-nocam", email="nocam@example.com", password_hash="x", role="user", is_active=True)
    owner.groups.append(group)
    db_session.add(owner)
    await db_session.commit()
    raw = await _key(db_session, owner=owner)

    async with _headerless(async_client) as client:
        response = await client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", headers={"X-API-Key": raw})

    assert response.status_code == 403
    capture.assert_not_awaited()


async def test_a_printer_that_took_the_id_is_not_served(async_client, db_session, printer_factory, capture):
    printer = await printer_factory()
    raw = await _key(db_session)

    async with _headerless(async_client) as client:
        response = await client.get(
            f"/api/v1/printers/{printer.id}/camera/snapshot",
            headers={"X-API-Key": raw, "X-BamDude-Printer-Identity": "00000000-0000-4000-8000-000000000000"},
        )

    assert response.status_code == 404 and response.json()["detail"]["error"] == "printer_not_found"
    capture.assert_not_awaited()


async def test_the_token_door_is_unchanged(async_client, printer_factory, capture):
    printer = await printer_factory()
    token = await create_camera_stream_token(authority=await mint_test_authority())

    async with _headerless(async_client) as client:
        with_token = await client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", params={"token": token})
        without = await client.get(f"/api/v1/printers/{printer.id}/camera/snapshot")

    assert with_token.status_code == 200 and with_token.content == JPEG
    assert without.status_code == 401


@pytest.mark.parametrize("deadline", ["credential", "absolute_lease"])
async def test_an_api_key_stream_ends_at_its_captured_deadline(
    async_client, db_session, printer_factory, monkeypatch, deadline
):
    from backend.app.api.routes import camera
    from backend.app.services import live_authority

    printer = await printer_factory()
    raw = await _key(db_session, expires_at=utcnow() + timedelta(seconds=2) if deadline == "credential" else None)
    await db_session.close()
    if deadline == "absolute_lease":
        monkeypatch.setattr(camera, "API_KEY_STREAM_LEASE_SECONDS", 0.4)
        monkeypatch.setattr(live_authority, "RECHECK_SECONDS", 0.05)
    proofs = []
    original_read = live_authority.LiveAuthority._read

    async def read(self, batch):
        proofs.extend(handle.descriptor.expires for handle in batch)
        return await original_read(self, batch)

    monkeypatch.setattr(live_authority.LiveAuthority, "_read", read)

    class FakeRuntime:
        async def stream_builtin(self, *, on_frame, disconnect_event, **_kwargs):
            while not disconnect_event.is_set():
                on_frame(JPEG)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"
                await asyncio.sleep(0.05)

    monkeypatch.setattr("backend.app.services.camera_runtime.get_camera_runtime", lambda: FakeRuntime())

    req = make_request(f"/api/v1/printers/{printer.id}/camera/stream", headers=[(b"x-api-key", raw.encode())])
    req.scope["http_version"] = "1.1"
    req.scope["root_path"] = ""
    received = []

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body":
            received.append(message["body"])

    async def receive():
        await asyncio.Event().wait()

    # httpx ASGITransport requires a terminal chunk; a retired media response
    # deliberately closes without one. Exercise actual ASGI delivery instead.
    await asyncio.wait_for(async_client._transport.app(req.scope, receive, send), timeout=6)
    assert b"".join(received)
    assert proofs and len(set(proofs)) == 1
    if deadline == "absolute_lease":
        assert len(proofs) >= 2, "freshness rechecks must not renew the original absolute lease"
    await camera.shutdown_broadcaster(f"printer-{printer.id}")
