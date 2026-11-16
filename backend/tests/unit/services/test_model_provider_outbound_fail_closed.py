"""The address pin fails CLOSED (review MB-I1).

A connection may only be opened by the transport's own socket layer, to an address it checked.
Nothing here trusts a private attribute of the HTTP library: each test puts a client library that
"moved on" under the transport and looks for the forbidden thing — a request that left unpinned.

No network: sockets are ``tests/_fixtures/wire.WireSockets``.
"""

from __future__ import annotations

import httpcore
import httpx
import pytest

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.outbound import GuardedTransport, OutboundRefused, PinnedOrigin
from backend.tests._fixtures.wire import WireSockets

ADDRESS = "52.216.1.1"
DNS = {"a.example.com": [ADDRESS]}


@pytest.fixture(autouse=True)
def scripted_dns(monkeypatch):
    async def resolve(host, port):
        if host not in DNS:
            raise OSError("unscripted")
        return list(DNS[host])

    monkeypatch.setattr(outbound, "resolve_host", resolve)


@pytest.fixture
def unproven(monkeypatch):
    """The process has not proven its socket layer yet (and forgets this test's verdict afterwards)."""
    monkeypatch.setattr(outbound, "_pinning_proven", None, raising=False)


def ok(_request):
    return httpx.Response(200, content=b"ok")


def origin() -> PinnedOrigin:
    return PinnedOrigin.admit("https://a.example.com", label="x")


async def test_a_client_library_that_keeps_its_pool_elsewhere_cannot_send_unpinned(monkeypatch):
    """httpx's own transport that ignores a replaced ``_pool`` — what an upgrade could bring."""
    unpinned: list[str] = []

    class Drifted(httpx.AsyncBaseTransport):
        def __init__(self, **_kwargs):
            pass

        async def handle_async_request(self, request):
            unpinned.append(str(request.url))  # it would resolve the name itself and connect
            return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Drifted)
    sockets = WireSockets(ok)
    transport = GuardedTransport(origin(), sockets=sockets)
    answer = await transport.get("/models", bearer="tok")
    await transport.aclose()
    assert unpinned == [], "a request left through a client that never asked our socket layer"
    assert answer.status == 200 and sockets.addresses() == [(ADDRESS, 443)]


async def test_a_transport_that_is_not_the_pinning_one_is_never_used(monkeypatch):
    """Whatever builds the HTTP layer, only the pinning transport is accepted — no ordinary client."""
    sent: list[str] = []

    def ordinary(request):
        sent.append(str(request.url))
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(outbound, "_pinned_http", lambda *args, **kwargs: httpx.MockTransport(ordinary))
    transport = GuardedTransport(origin())
    with pytest.raises(OutboundRefused) as caught:
        await transport.get("/models", bearer="tok")
    await transport.aclose()
    assert caught.value.reason == "transport_disabled"
    assert sent == []


async def test_a_request_nobody_pinned_opens_no_socket_through_the_whole_client():
    sockets = WireSockets(ok)
    transport = GuardedTransport(origin(), sockets=sockets)
    await transport.get("/models")  # the client exists and works
    client = await transport._http()
    with pytest.raises((httpx.HTTPError, OutboundRefused)):
        await client.send(httpx.Request("GET", "https://unchecked.example.com/x"))
    with pytest.raises((httpx.HTTPError, OutboundRefused)):
        # Even the name that WAS checked a moment ago: the pin belongs to one request.
        await client.send(httpx.Request("GET", "https://a.example.com:8443/x"))
    await transport.aclose()
    assert sockets.attempts == [(ADDRESS, 443)]


async def test_a_socket_layer_the_library_ignores_disables_the_transport(monkeypatch, unproven):
    """The first use proves the mechanism with a sentinel; when the proof fails nothing is sent."""
    rogue = WireSockets(ok)
    real_pool = httpcore.AsyncConnectionPool

    def pool_ignoring_our_backend(**kwargs):
        kwargs["network_backend"] = rogue  # the library connects by itself, past our socket layer
        return real_pool(**kwargs)

    monkeypatch.setattr(httpcore, "AsyncConnectionPool", pool_ignoring_our_backend)
    transport = GuardedTransport(origin(), sockets=WireSockets(ok))
    for _ in range(2):  # the verdict is remembered: the second request is refused as well
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models", bearer="tok")
        assert caught.value.reason == "transport_disabled"
    await transport.aclose()
    # Only the sentinel ever left: never the configured name, never the token.
    assert all(host == outbound.PROBE_HOST for host, _port in rogue.attempts)
    assert all(b"authorization" not in name.lower() for request in rogue.requests() for name, _ in request.headers.raw)
    assert outbound.transport_disabled() is True


async def test_the_proof_runs_once_and_reaches_only_our_socket_layer(monkeypatch, unproven):
    probes: list[int] = []
    real = outbound._prove_pinning

    async def counted():
        probes.append(1)
        return await real()

    monkeypatch.setattr(outbound, "_prove_pinning", counted)
    sockets = WireSockets(ok)
    transport = GuardedTransport(origin(), sockets=sockets)
    for _ in range(3):
        await transport.get("/models")
    await transport.aclose()
    assert probes == [1]
    assert outbound.transport_disabled() is False
    # The sentinel went to the proof's own stub, not to this transport's sockets.
    assert sockets.attempts == [(ADDRESS, 443)]


def test_no_application_code_hands_the_transport_a_client_of_its_own():
    """``client=`` / ``sockets=`` are test seams: production builds the pinning client itself."""
    import ast
    from pathlib import Path

    app = Path(outbound.__file__).resolve().parents[2]
    offenders = []
    for path in app.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "GuardedTransport"
                and any(keyword.arg in ("client", "sockets") for keyword in node.keywords)
            ):
                offenders.append(str(path.relative_to(app)))
    assert offenders == []
