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
