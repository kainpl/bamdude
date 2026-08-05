"""Integration tests for the webhook API (`/api/v1/webhook/*`).

This surface had no tests at all. It is the one part of BamDude whose callers
are *outside* the repo — an automation holding an API key — so nothing in the
frontend exercises it and nothing would have caught a regression. Settings →
API keys advertises it to users, which makes it a contract.

The three things worth pinning are the three ways an external caller gets it
wrong: no key, a key without the right scope, and a key scoped to a different
printer. Behaviour is checked at the edges the endpoints actually guard —
a printer that is not connected, a queue with nothing in it.
"""

import pytest
from httpx import AsyncClient

from backend.app.core.auth import create_access_token


async def _key(async_client: AsyncClient, **flags) -> str:
    """Create an API key and return the raw secret."""
    payload = {"name": flags.pop("name", "hook"), **flags}
    response = await async_client.post("/api/v1/api-keys/", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["key"]


class _NoJWT:
    """Drop the JWT for the duration of a call.

    Every test here must reach the route through the API key alone: with the
    session's admin bearer still attached, an endpoint would pass on the JWT
    and the key's own scopes would never be consulted — the test would prove
    nothing about the thing it names.
    """

    def __init__(self, client: AsyncClient):
        self.client = client

    def __enter__(self):
        self.saved = self.client.headers.pop("Authorization", None)
        return self.client

    def __exit__(self, *exc):
        self.client.headers["Authorization"] = self.saved or (
            f"Bearer {create_access_token(data={'sub': 'test_admin'})}"
        )


@pytest.mark.asyncio
@pytest.mark.integration
class TestWebhookAuth:
    async def test_refuses_a_call_with_no_api_key(self, async_client: AsyncClient):
        with _NoJWT(async_client) as client:
            response = await client.get("/api/v1/webhook/queue")
        assert response.status_code in (401, 403)

    async def test_refuses_control_when_the_key_may_only_read(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        raw = await _key(async_client, name="reader", can_read_status=True, can_control_printer=False)

        with _NoJWT(async_client) as client:
            response = await client.post(f"/api/v1/webhook/printer/{printer.id}/stop", headers={"X-API-Key": raw})

        assert response.status_code == 403

    async def test_refuses_a_printer_the_key_is_not_scoped_to(self, async_client: AsyncClient, printer_factory):
        allowed = await printer_factory()
        other = await printer_factory()
        raw = await _key(
            async_client,
            name="scoped",
            can_read_status=True,
            can_control_printer=True,
            printer_ids=[allowed.id],
        )

        with _NoJWT(async_client) as client:
            response = await client.get(f"/api/v1/webhook/printer/{other.id}/status", headers={"X-API-Key": raw})

        assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
class TestWebhookRead:
    async def test_reports_a_printers_status(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="Reportable")
        raw = await _key(async_client, name="r", can_read_status=True)

        with _NoJWT(async_client) as client:
            response = await client.get(f"/api/v1/webhook/printer/{printer.id}/status", headers={"X-API-Key": raw})

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == printer.id
        assert body["name"] == "Reportable"
        # No MQTT in tests, so the printer is legitimately not connected — the
        # endpoint must still answer rather than fail on a missing live status.
        assert body["connected"] is False

    async def test_unknown_printer_is_404_not_an_empty_status(self, async_client: AsyncClient):
        raw = await _key(async_client, name="r", can_read_status=True)

        with _NoJWT(async_client) as client:
            response = await client.get("/api/v1/webhook/printer/99999/status", headers={"X-API-Key": raw})

        assert response.status_code == 404

    async def test_queue_status_lists_a_printer_with_nothing_queued(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="Idle one")
        raw = await _key(async_client, name="r", can_read_status=True)

        with _NoJWT(async_client) as client:
            response = await client.get("/api/v1/webhook/queue", headers={"X-API-Key": raw})

        assert response.status_code == 200
        rows = {row["printer_id"]: row for row in response.json()}
        assert printer.id in rows, "a printer with an empty queue still has a queue"
        assert rows[printer.id]["pending"] == 0
        assert rows[printer.id]["printing"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
class TestWebhookControl:
    async def test_start_says_so_when_the_queue_is_empty(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        raw = await _key(async_client, name="c", can_control_printer=True)

        with _NoJWT(async_client) as client:
            response = await client.post(f"/api/v1/webhook/printer/{printer.id}/start", headers={"X-API-Key": raw})

        assert response.status_code == 404
        assert "pending" in response.json()["detail"].lower()

    async def test_stop_refuses_a_disconnected_printer(self, async_client: AsyncClient, printer_factory):
        # 503, not 500: the printer is unreachable, which is a state the caller
        # can retry, not a bug in the request.
        printer = await printer_factory()
        raw = await _key(async_client, name="c", can_control_printer=True)

        with _NoJWT(async_client) as client:
            response = await client.post(f"/api/v1/webhook/printer/{printer.id}/stop", headers={"X-API-Key": raw})

        assert response.status_code == 503

    async def test_cancel_refuses_a_disconnected_printer(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        raw = await _key(async_client, name="c", can_control_printer=True)

        with _NoJWT(async_client) as client:
            response = await client.post(f"/api/v1/webhook/printer/{printer.id}/cancel", headers={"X-API-Key": raw})

        assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_router_exposes_exactly_the_documented_surface():
    """Settings → API keys lists these five and only these five.

    The panel drifted from the code once already — it advertised
    ``/webhook/status``, ``/webhook/status/:id``, ``/printer/:id/pause`` and
    ``/printer/:id/resume``, none of which ever existed, so five of its six
    entries answered 404. This fails the moment the two lists part again.
    """
    from backend.app.api.routes.webhook import router

    surface = {(sorted(r.methods - {"HEAD", "OPTIONS"})[0], r.path) for r in router.routes}

    assert surface == {
        ("GET", "/webhook/queue"),
        ("GET", "/webhook/printer/{printer_id}/status"),
        ("POST", "/webhook/printer/{printer_id}/start"),
        ("POST", "/webhook/printer/{printer_id}/stop"),
        ("POST", "/webhook/printer/{printer_id}/cancel"),
    }
