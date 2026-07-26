"""WebSocket connection-token auth gate (upstream Bambuddy GHSA-r2qv follow-up).

``/api/v1/ws`` used to accept any client that could reach the HTTP port and
immediately fan every ``printer_status`` / ``print_*`` / ``archive_*`` /
``inventory_*`` broadcast out to it — the ``@app.middleware("http")`` auth gate
only runs on the "http" scope and never sees the WebSocket upgrade. The endpoint
now requires a short-lived token minted via ``POST /api/v1/auth/ws-token``
(behind ``Permission.WEBSOCKET_CONNECT``) and passed as ``?token=``.

The endpoint gate is a one-liner (``if not await verify_websocket_token(token):
close(4401)``); these tests pin the token round-trip it depends on and the
auth on the mint endpoint.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_token_endpoint_mints_a_token(async_client: AsyncClient):
    resp = await async_client.post("/api/v1/auth/ws-token")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("token"), str) and body["token"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_token_endpoint_requires_authentication(async_client: AsyncClient):
    """Always-on auth: an unauthenticated caller cannot mint a WS token."""
    resp = await async_client.post("/api/v1/auth/ws-token", headers={"Authorization": ""})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_websocket_token_roundtrip_and_rejection(async_client: AsyncClient):
    """A freshly minted token verifies; bogus / empty tokens do not — this is
    exactly what the ``/ws`` gate checks before ``ws_manager.connect``."""
    from backend.app.core.auth import create_websocket_token, verify_websocket_token

    token = await create_websocket_token()
    assert await verify_websocket_token(token) is True
    assert await verify_websocket_token("not-a-real-token") is False
    assert await verify_websocket_token("") is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_websocket_token_is_scoped_to_its_own_type(async_client: AsyncClient):
    """A camera-stream token must NOT be accepted by the WebSocket verifier
    (token_type isolation) and vice-versa — no cross-token confusion."""
    from backend.app.core.auth import (
        create_camera_stream_token,
        verify_camera_stream_token,
        verify_websocket_token,
    )

    camera_token = await create_camera_stream_token()
    assert await verify_websocket_token(camera_token) is False
    assert await verify_camera_stream_token(camera_token) is True


class _FakeWS:
    """Minimal WebSocket stand-in recording what the manager sends it."""

    def __init__(self):
        self.sent: list[str] = []

    async def accept(self):
        pass

    async def send_text(self, data: str):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_broadcast_to_user_targets_only_the_owner():
    """Owner-scoped fan-out: a per-user broadcast reaches only that user's
    connections; ``user_id=None`` falls back to a global broadcast (BamDude has
    no anonymous users, so per-user targeting is the norm)."""
    from backend.app.core.websocket import ConnectionManager

    mgr = ConnectionManager()
    a, b = _FakeWS(), _FakeWS()
    await mgr.connect(a, user_id=1)
    await mgr.connect(b, user_id=2)

    await mgr.broadcast_to_user(1, {"type": "spool_assignment_verified"})
    assert len(a.sent) == 1 and len(b.sent) == 0  # only user 1's connection

    await mgr.broadcast_to_user(None, {"type": "global"})  # None → global fallback
    assert len(a.sent) == 2 and len(b.sent) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_token_resolves_to_minting_user(async_client: AsyncClient):
    """The /ws-token endpoint records the authenticated user on the token so the
    connection can be tagged for per-user broadcasts; a token minted without a
    user (API-key caller) resolves to None (→ global fallback)."""
    from backend.app.core.auth import create_websocket_token, resolve_websocket_token_user

    resp = await async_client.post("/api/v1/auth/ws-token")
    uid = await resolve_websocket_token_user(resp.json()["token"])
    assert isinstance(uid, int) and uid > 0

    assert await resolve_websocket_token_user(await create_websocket_token()) is None  # userless
    assert await resolve_websocket_token_user("not-a-real-token") is None
