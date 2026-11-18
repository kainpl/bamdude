"""The transport's real client: a connection belongs to the host NAME it was verified for.

The transport resolves a name, checks every address and connects to a checked one. The request
URL keeps the name, so the connection pool, the TLS server name and certificate verification are
all by name: two names that share an address (shared hosting, a CDN, a storage host) never share
a connection, and a certificate that does not match the second name fails that request.

No network: the socket layer is a stub that records every connection opened and every TLS
handshake asked for, and answers HTTP/1.1 from a canned response.
"""

from __future__ import annotations

import ssl

import httpcore
import pytest

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.outbound import GuardedTransport, OutboundRefused, PinnedOrigin

SHARED_IP = "52.216.1.1"
DNS = {"a.example.com": [SHARED_IP], "b.example.com": [SHARED_IP], "lan.example.com": ["192.168.1.20"]}


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self, sockets, address, answers):
        self.sockets, self.address, self.answers = sockets, address, answers
        self.server_hostname: str | None = None
        self.requests: list[bytes] = []
        self._pending = b""

    async def read(self, max_bytes, timeout=None):
        data, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self.requests.append(bytes(buffer))
        if b"\r\n\r\n" in buffer:
            host = next(line for line in bytes(buffer).split(b"\r\n") if line.lower().startswith(b"host:"))
            self._pending += self.answers(host.split(b":", 1)[1].strip().decode(), bytes(buffer))

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # What a real handshake does with a certificate issued for another name.
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname is True
        self.server_hostname = server_hostname
        if server_hostname in self.sockets.certificate_mismatch:
            raise httpcore.ConnectError(f"certificate verify failed: hostname mismatch for {server_hostname}")
        return self

    def get_extra_info(self, info):
        return None


class Sockets(httpcore.AsyncNetworkBackend):
    """The stubbed socket layer."""

    def __init__(self, answers):
        self.answers = answers
        self.opened: list[_Stream] = []
        self.certificate_mismatch: set[str] = set()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = _Stream(self, (host, port), self.answers)
        self.opened.append(stream)
        return stream

    async def sleep(self, seconds):
        pass


def ok(_host, _request):
    return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok"


@pytest.fixture(autouse=True)
def scripted_dns(monkeypatch):
    async def resolve(host, port):
        if host not in DNS:
            raise OSError("unscripted")
        return list(DNS[host])

    monkeypatch.setattr(outbound, "resolve_host", resolve)


def transport_over(sockets: Sockets, base: str) -> GuardedTransport:
    return GuardedTransport(PinnedOrigin.admit(base, label="x"), sockets=sockets)


async def test_the_socket_goes_to_the_checked_address_and_tls_is_verified_for_the_name():
    sockets = Sockets(ok)
    transport = transport_over(sockets, "https://a.example.com")
    answer = await transport.get("/models", bearer="tok")
    await transport.aclose()
    assert answer.status == 200
    (stream,) = sockets.opened
    assert stream.address == (SHARED_IP, 443)
    assert stream.server_hostname == "a.example.com"
    head = stream.requests[0].lower()
    assert b"host: a.example.com\r\n" in head and b"authorization: bearer tok" in head


async def test_two_names_on_one_address_never_share_a_connection():
    def answers(host, request):
        if host == "a.example.com" and b"/raw/" in request:
            return b"HTTP/1.1 302 Found\r\nLocation: https://b.example.com/k/a.stl\r\nContent-Length: 0\r\n\r\n"
        return ok(host, request)

    sockets = Sockets(answers)
    transport = transport_over(sockets, "https://a.example.com")
    await transport.get("/models", bearer="tok")  # a kept-alive connection verified for a.example.com
    async with transport.stream("/models/m/raw/a.stl", bearer="tok", foreign_hop=True) as answer:
        body = b"".join([chunk async for chunk in answer.chunks(1024)])
    await transport.aclose()
    assert (answer.status, answer.foreign, body) == (200, True, b"ok")
    by_name = {stream.server_hostname: stream for stream in sockets.opened}
    assert set(by_name) == {"a.example.com", "b.example.com"}, "the second name needs a handshake of its own"
    assert len(sockets.opened) == 2
    # No connection opened for one name carried a request for the other.
    for name, stream in by_name.items():
        hosts = {
            line.split(b":", 1)[1].strip().decode()
            for request in stream.requests
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"host:")
        }
        assert hosts == {name}
    assert all(b"authorization" not in request.lower() for request in by_name["b.example.com"].requests)


async def test_a_certificate_for_another_name_fails_the_second_name():
    def answers(host, request):
        if b"/raw/" in request:
            return b"HTTP/1.1 302 Found\r\nLocation: https://b.example.com/k/a.stl\r\nContent-Length: 0\r\n\r\n"
        return ok(host, request)

    sockets = Sockets(answers)
    sockets.certificate_mismatch = {"b.example.com"}
    transport = transport_over(sockets, "https://a.example.com")
    await transport.get("/models", bearer="tok")
    with pytest.raises(OutboundRefused) as caught:
        async with transport.stream("/models/m/raw/a.stl", bearer="tok", foreign_hop=True):
            pass
    await transport.aclose()
    assert caught.value.reason == "storage_unreachable"
    assert [stream.server_hostname for stream in sockets.opened] == ["a.example.com", "b.example.com"]


async def test_a_host_nobody_checked_gets_no_socket():
    """The socket layer itself refuses a name the transport did not resolve and check."""
    sockets = Sockets(ok)
    transport = transport_over(sockets, "https://a.example.com")
    layer = (await transport._http())._transport
    assert isinstance(layer, outbound._PinnedHTTP)
    with pytest.raises(httpcore.ConnectError):
        await layer.sockets.connect_tcp("unchecked.example.com", 443)
    await transport.aclose()
    assert sockets.opened == []


async def test_plain_http_to_a_lan_address_is_pinned_too():
    sockets = Sockets(ok)
    transport = transport_over(sockets, "http://lan.example.com:3214")
    await transport.get("/models", bearer="tok")
    await transport.aclose()
    (stream,) = sockets.opened
    assert stream.address == ("192.168.1.20", 3214) and stream.server_hostname is None
    assert b"host: lan.example.com:3214\r\n" in stream.requests[0].lower()


async def test_the_default_client_verifies_certificates_and_ignores_the_environment():
    transport = GuardedTransport(PinnedOrigin.admit("https://a.example.com", label="x"))
    client = await transport._http()
    context = client._transport.ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert client.trust_env is False
    assert client.follow_redirects is False
    await transport.aclose()


async def test_the_handshake_gets_the_verifying_context():
    """What the socket layer is actually handed for TLS — not what an attribute says."""
    import httpx

    from backend.tests._fixtures.wire import WireSockets

    sockets = WireSockets(lambda request: httpx.Response(200, content=b"ok"))
    transport = GuardedTransport(PinnedOrigin.admit("https://a.example.com", label="x"), sockets=sockets)
    await transport.get("/models")
    await transport.aclose()
    (context,) = sockets.tls_contexts
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert sockets.opened[0].server_hostname == "a.example.com"


async def test_the_trust_store_is_built_once_per_process_and_never_on_the_event_loop(monkeypatch):
    """Review MB-I3: every request used to build a TLS context (~0.3 s) inside the loop."""
    import threading

    import httpx

    from backend.tests._fixtures.wire import WireSockets

    built: list[int] = []
    real = ssl.create_default_context

    def counted(*args, **kwargs):
        built.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counted)
    monkeypatch.setattr(outbound, "_tls_cache", {}, raising=False)
    contexts = []
    for _ in range(6):  # six API requests, each with a service — and a transport — of its own
        sockets = WireSockets(lambda request: httpx.Response(200, content=b"ok"))
        transport = GuardedTransport(PinnedOrigin.admit("https://a.example.com", label="x"), sockets=sockets)
        await transport.get("/models")
        await transport.get("/models")
        await transport.aclose()
        contexts += sockets.tls_contexts
    assert len(built) == 1, f"{len(built)} trust stores built for 6 transports"
    assert built[0] != threading.get_ident(), "the trust store was built on the event loop's thread"
    assert len(contexts) == 6 and len({id(context) for context in contexts}) == 1
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED and contexts[0].check_hostname is True
