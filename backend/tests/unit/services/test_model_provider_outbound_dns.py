"""Name resolution of the configured-origin transport (review MB-M6)."""

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


class TestResolution:
    async def test_names_are_resolved_on_the_transports_own_small_pool(self, monkeypatch):
        threads: list[str] = []

        def getaddrinfo(host, port, *args, **kwargs):
            threads.append(threading.current_thread().name)
            return answers_for(V4, V4)(host, port)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        assert await outbound.resolve_host("files.example.com", 443) == [V4]
        assert threads and all(name.startswith("model-source-dns") for name in threads), threads

    async def test_a_resolver_that_does_not_answer_is_given_up_on(self, monkeypatch):
        def slow(host, port, *args, **kwargs):
            time.sleep(3.0)
            return answers_for(V4)(host, port)

        monkeypatch.setattr(socket, "getaddrinfo", slow)
        monkeypatch.setattr(outbound, "DNS_DEADLINE_SECONDS", 0.05, raising=False)
        started = time.monotonic()
        transport = GuardedTransport(PinnedOrigin.admit("https://slow.example.com", label="x"), sockets=WireSockets(ok))
        with pytest.raises(OutboundRefused) as caught:
            await transport.get("/models")
        await transport.aclose()
        assert caught.value.reason == "unreachable" and time.monotonic() - started < 2.0
