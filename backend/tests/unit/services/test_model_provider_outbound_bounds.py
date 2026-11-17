"""Byte caps are on the WIRE (review MB-I2).

The transport asks for identity encoding and refuses an answer that is compressed anyway: a cap
counted after inflation let a 300 KB gzip body allocate ~140 MiB against a 4 MiB limit. Every
case runs the real client over stub sockets and serves raw bytes, so what is measured is what a
hostile server can make this process hold.
"""

from __future__ import annotations

import gzip
import tracemalloc
import zlib

import httpx
import pytest

from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.manyfold import service as manyfold
from backend.app.services.model_providers.manyfold.config import ManyfoldConfig
from backend.app.services.model_providers.manyfold.service import ManyfoldService, ManyfoldUnavailable
from backend.app.services.model_providers.outbound import (
    GuardedTransport,
    OutboundRefused,
    OutboundTooLarge,
    PinnedOrigin,
)
from backend.tests._fixtures.manyfold import BASE, CLIENT_ID, CLIENT_SECRET, MODEL, FakeManyfold
from backend.tests._fixtures.wire import WireSockets, serialise

MIB = 1024 * 1024
DNS = {"a.example.com": ["52.216.1.1"], "store.example.com": ["52.216.1.9"], "manyfold.lan": ["192.168.1.20"]}
BOMB = gzip.compress(b"\x00" * (96 * MIB))  # ~93 KB on the wire
#: What one request may make this process hold on top of its cap-sized buffer.
HEADROOM = 6 * MIB


@pytest.fixture(autouse=True)
def scripted_dns(monkeypatch):
    async def resolve(host, port):
        if host not in DNS:
            raise OSError("unscripted")
        return list(DNS[host])

    monkeypatch.setattr(outbound, "resolve_host", resolve)
    manyfold.clear_caches()
    yield
    manyfold.clear_caches()


def bomb(encoding: bytes = b"gzip", body: bytes = BOMB, content_type: bytes = b"application/json") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type + b"\r\nContent-Encoding: " + encoding + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )


async def peak_of(call) -> tuple[BaseException | None, int]:
    tracemalloc.start()
    try:
        try:
            await call()
            raised = None
        except BaseException as exc:
            raised = exc
        return raised, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def transport_over(sockets: WireSockets, base: str = "https://a.example.com") -> GuardedTransport:
    return GuardedTransport(PinnedOrigin.admit(base, label="x"), sockets=sockets)


async def test_every_request_asks_for_an_uncompressed_answer():
    sockets = WireSockets(lambda request: httpx.Response(200, content=b"{}"))
    transport = transport_over(sockets)
    await transport.get("/models", bearer="tok")
    await transport.post_form("/oauth/token", {"a": "b"})
    async with transport.stream("/models/m/raw/a.stl", bearer="tok", foreign_hop=True):
        pass
    await transport.aclose()
    assert [request.headers.get("accept-encoding") for request in sockets.requests()] == ["identity"] * 3


@pytest.mark.parametrize("encoding", [b"gzip", b"deflate", b"GZip", b"gzip, identity", b"br", b"x-unknown"])
async def test_a_compressed_json_answer_is_refused_unread(encoding):
    body = zlib.compress(b"\x00" * (96 * MIB)) if encoding == b"deflate" else BOMB
    sockets = WireSockets(lambda request: bomb(encoding, body))
    transport = transport_over(sockets)
    raised, peak = await peak_of(lambda: transport.get("/models", bearer="tok", cap=4 * MIB))
    await transport.aclose()
    assert isinstance(raised, OutboundRefused) and raised.reason == "encoded", repr(raised)
    assert peak < HEADROOM, f"{peak / MIB:.1f} MiB held for one refused answer"


async def test_a_compressed_token_answer_is_refused_unread():
    sockets = WireSockets(lambda request: bomb())
    transport = transport_over(sockets)
    raised, peak = await peak_of(lambda: transport.post_form("/oauth/token", {"client_secret": "s"}))
    await transport.aclose()
    assert isinstance(raised, OutboundRefused) and raised.reason == "encoded"
    assert peak < HEADROOM


async def test_a_compressed_file_from_storage_is_refused_unread():
    def answers(request):
        if request.url.host == "a.example.com":
            return httpx.Response(302, headers={"Location": "https://store.example.com/k/a.stl"})
        return bomb(content_type=b"model/stl")

    sockets = WireSockets(answers)
    transport = transport_over(sockets)

    async def read():
        async with transport.stream("/models/m/raw/a.stl", bearer="tok", foreign_hop=True) as answer:
            async for _chunk in answer.chunks(1024 * MIB):
                pass

    raised, peak = await peak_of(read)
    await transport.aclose()
    assert isinstance(raised, OutboundRefused) and raised.reason == "encoded"
    assert peak < HEADROOM


async def test_an_identity_answer_is_read_as_it_is():
    sockets = WireSockets(lambda request: bomb(b"identity", b'{"a": 1}'))
    transport = transport_over(sockets)
    answer = await transport.get("/models")
    await transport.aclose()
    assert answer.json() == {"a": 1}


async def test_the_cap_counts_the_bytes_of_the_wire_without_a_declared_length():
    frame = b"10000\r\n" + b"x" * 65536 + b"\r\n"
    wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + frame * 24 + b"0\r\n\r\n"
    sockets = WireSockets(lambda request: wire)
    transport = transport_over(sockets)
    raised, peak = await peak_of(lambda: transport.get("/models", cap=1 * MIB))
    await transport.aclose()
    assert isinstance(raised, OutboundTooLarge)
    assert peak < 1 * MIB + HEADROOM


class TestManyfoldPaths:
    """The same refusal through the Manyfold client: list, sign-in, preview and file."""

    def service(self, sockets: WireSockets) -> ManyfoldService:
        config = ManyfoldConfig(url=BASE, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, enabled=True)
        transport = GuardedTransport(PinnedOrigin.admit(BASE, label="Manyfold URL"), sockets=sockets)
        return ManyfoldService(config, transport=transport)

    def hostile(self, where) -> WireSockets:
        fake = FakeManyfold()

        def answers(request: httpx.Request):
            return bomb() if where(request) else serialise(fake.handle(request))

        return WireSockets(answers)

    @pytest.mark.parametrize(
        ("where", "call"),
        [
            (lambda r: r.url.path == "/oauth/token", lambda s: s.list_models()),
            (lambda r: r.url.path == "/models", lambda s: s.list_models()),
            (lambda r: "derivative" in str(r.url), lambda s: s.fetch_preview(MODEL)),
        ],
        ids=["sign-in", "list", "preview"],
    )
    async def test_a_compressed_answer_is_an_unreadable_manyfold(self, where, call):
        service = self.service(self.hostile(where))
        raised, peak = await peak_of(lambda: call(service))
        await service.close()
        assert isinstance(raised, ManyfoldUnavailable) and raised.code == "manyfold_incompatible", repr(raised)
        assert peak < HEADROOM

    async def test_a_compressed_file_is_not_written(self, tmp_path):
        service = self.service(self.hostile(lambda r: "/raw/" in r.url.path))
        file = await service.get_file(MODEL, "f1c2d3g4h5j6")
        target = tmp_path / "a.stl"
        raised, peak = await peak_of(lambda: service.download_file(file, target))
        await service.close()
        assert isinstance(raised, ManyfoldUnavailable) and raised.code == "manyfold_incompatible"
        assert peak < HEADROOM
        assert not target.exists() or target.stat().st_size == 0
