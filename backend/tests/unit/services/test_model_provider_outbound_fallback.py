"""A connection may fall back to the next CHECKED address of a name (review MB-M7)."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.outbound import GuardedTransport, OutboundRefused, PinnedOrigin
from backend.tests._fixtures.wire import WireSockets

V6, V4 = "2600:1f18::1", "52.216.1.1"


def ok(_request):
    return httpx.Response(200, content=b"ok")


def answers_for(*addresses: str):
    def getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return getaddrinfo


class TestTheNextCheckedAddress:
    @pytest.fixture(autouse=True)
    def dual_stack(self, monkeypatch):
        self.dns = {"dual.example.com": [V6, V4], "trap.example.com": [V6, "169.254.169.254"]}

        async def resolve(host, port):
            return list(self.dns[host])

        monkeypatch.setattr(outbound, "resolve_host", resolve)

    async def test_a_dead_first_address_falls_back_to_the_next_checked_one(self):
        sockets = WireSockets(ok)
        sockets.refuse = {V6}  # a host without working IPv6
        transport = GuardedTransport(PinnedOrigin.admit("https://dual.example.com", label="x"), sockets=sockets)
        answer = await transport.post_form("/oauth/token", {"client_secret": "s"})
        await transport.aclose()
        assert answer.status == 200
        assert sockets.attempts == [(V6, 443), (V4, 443)]
        (stream,) = sockets.opened
        assert stream.address == (V4, 443) and stream.server_hostname == "dual.example.com"
        assert stream.requests[0].content == b"client_secret=s"

    async def test_every_address_dead_is_unreachable(self):
        sockets = WireSockets(ok)
        sockets.refuse = {V6, V4}
        transport = GuardedTransport(PinnedOrigin.admit("https://dual.example.com", label="x"), sockets=sockets)
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models")
        await transport.aclose()
        assert caught.value.reason == "unreachable" and sockets.attempts == [(V6, 443), (V4, 443)]

    async def test_an_unchecked_address_is_never_a_fallback(self):
        """One refused answer refuses the name: the dead first address does not lead to the second."""
        sockets = WireSockets(ok)
        sockets.refuse = {V6}
        transport = GuardedTransport(PinnedOrigin.admit("https://trap.example.com", label="x"), sockets=sockets)
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models")
        await transport.aclose()
        assert caught.value.reason == "address_refused" and sockets.attempts == []

    async def test_a_failure_after_the_connection_is_not_retried_elsewhere(self):
        """Only a failed CONNECT moves on: a request that reached a server is never sent twice."""
        sockets = WireSockets(lambda request: b"garbage that is not HTTP\r\n\r\n")
        transport = GuardedTransport(PinnedOrigin.admit("https://dual.example.com", label="x"), sockets=sockets)
        with pytest.raises(OutboundRefused):
            await transport.post_form("/oauth/token", {"client_secret": "s"})
        await transport.aclose()
        assert sockets.attempts == [(V6, 443)]
