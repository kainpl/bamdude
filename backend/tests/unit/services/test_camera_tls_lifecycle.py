"""Production proxy lifecycle on real loopback sockets (never a printer).

Each scenario gets its own loop: Windows Proactor/Selector, Linux asyncio/uvloop.
Assertions precede fixture cleanup so the fixture cannot conceal a socket leak.
"""

import asyncio
import gc
import ssl
import sys
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.app.services import camera, camera_cleanup, camera_tls


def _loops():
    if sys.platform == "win32":
        return [asyncio.ProactorEventLoop, asyncio.SelectorEventLoop]
    try:
        import uvloop
    except ImportError:
        return [asyncio.new_event_loop, pytest.param(None, marks=pytest.mark.skip(reason="uvloop not installed"))]
    return [asyncio.new_event_loop, uvloop.new_event_loop]


@pytest.fixture(
    params=_loops(),
    ids=lambda factory: str(factory) if factory is None else factory.__module__ + ":" + factory.__qualname__,
)
def run(request):
    with asyncio.Runner(loop_factory=request.param) as runner:
        yield runner.run


@asynccontextmanager
async def silent_handshake_peer():
    peers = []
    connected = asyncio.Event()

    def accept(reader, writer):
        peers.append(writer)
        connected.set()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], connected
    finally:
        for writer in peers:
            writer.transport.abort()
        server.close()
        await server.wait_closed()


def test_close_cancels_stalled_handshake_and_closes_client(run):
    async def scenario():
        tasks_before = asyncio.all_tasks()
        async with silent_handshake_peer() as (target_port, connected):
            port, server = await camera.create_tls_proxy("127.0.0.1", target_port)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await asyncio.wait_for(connected.wait(), 1)
                await asyncio.wait_for(camera.close_tls_proxy(server), 0.5)
                assert await asyncio.wait_for(reader.read(), 0.5) == b""
            finally:
                writer.transport.abort()
                server.close()
                # Test isolation only, after assertions (also on baseline failure).
                pending = asyncio.all_tasks() - tasks_before
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    run(scenario())


@pytest.fixture
def tls_context(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


class TLSPeer(asyncio.Protocol):
    """Actual TLS over a raw loopback TCP peer; can deliberately ignore alerts."""

    def __init__(self, context, *, silent=False):
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.ssl = context.wrap_bio(self.incoming, self.outgoing, server_side=True)
        self.silent = silent
        self.ready = False
        self.received = bytearray()
        self.closed = asyncio.Event()

    def connection_made(self, transport):
        self.transport = transport

    def flush(self):
        if data := self.outgoing.read():
            self.transport.write(data)

    def send(self, data):
        self.ssl.write(data)
        self.flush()

    def data_received(self, data):
        if self.ready and self.silent:
            return
        self.incoming.write(data)
        try:
            if not self.ready:
                self.ssl.do_handshake()
                self.ready = True
                self.send(b"ready\n")
            while True:
                plain = self.ssl.read(65536)
                if not plain:
                    self.ssl.unwrap()
                    self.flush()
                    self.transport.close()
                    return
                self.received.extend(plain)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
        finally:
            self.flush()

    def connection_lost(self, exc):
        self.closed.set()


@asynccontextmanager
async def tls_farm(context, *, silent=False):
    peers, clients, proxies, errors = [], [], [], []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, event: errors.append(event))
    baseline = asyncio.all_tasks()

    def factory():
        peer = TLSPeer(context, silent=silent)
        peers.append(peer)
        return peer

    target = await loop.create_server(factory, "127.0.0.1", 0)

    async def connect(server=None):
        if server is None:
            port, server = await camera.create_tls_proxy("127.0.0.1", target.sockets[0].getsockname()[1])
            proxies.append(server)
        else:
            port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        clients.append(writer)
        assert await asyncio.wait_for(reader.readline(), 1) == b"ready\n"
        return server, reader, writer

    try:
        yield connect, peers, errors
    finally:
        # Deliberately AFTER the test's ownership/EOF assertions.
        for writer in clients:
            writer.transport.abort()
        for peer in peers:
            peer.transport.abort()
        for server in proxies:
            try:
                await camera.close_tls_proxy(server)
            except TimeoutError:
                pass
        target.close()
        await target.wait_closed()
        for task in asyncio.all_tasks() - baseline:
            task.cancel()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)


def assert_released(server):
    state = camera_tls._proxy_states[server]
    assert state.closed and not state.connections and state.failure is None
    assert not server.is_serving()


async def eventually(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def test_no_clients_repeated_close_and_weak_server_ownership(run):
    async def scenario():
        _, server = await camera.create_tls_proxy("127.0.0.1", 1)
        await camera.close_tls_proxy(server)
        await camera.close_tls_proxy(server)
        assert_released(server)
        ref = weakref.ref(server)
        del server
        await asyncio.sleep(0)
        gc.collect()
        assert ref() is None

    run(scenario())


@pytest.mark.parametrize("count", [1, 8])
def test_active_connections_close_in_parallel(run, tls_context, count):
    async def scenario():
        async with tls_farm(tls_context) as (connect, peers, errors):
            server, reader, _ = await connect()
            for _ in range(count - 1):
                await connect(server)
            await asyncio.wait_for(camera.close_tls_proxy(server), 2.5)
            assert_released(server)
            assert await reader.read() == b""
            await eventually(lambda: all(peer.closed.is_set() for peer in peers))
            assert not errors

    run(scenario())


@pytest.mark.parametrize("client_exits_first", [False, True])
def test_silent_close_notify_is_aborted_before_default_ssl_timer(run, tls_context, caplog, client_exits_first):
    async def scenario():
        async with tls_farm(tls_context, silent=True) as (connect, peers, errors):
            server, reader, writer = await connect()
            conn = next(iter(camera_tls._proxy_states[server].connections))
            if client_exits_first:
                writer.close()
                await writer.wait_closed()
                await eventually(lambda: conn.cleanup is not None)
            started = asyncio.get_running_loop().time()
            await camera.close_tls_proxy(server)
            assert asyncio.get_running_loop().time() - started < 2.3
            assert all(task.done() and not task.cancelled() for task in conn.writer_waits)
            assert_released(server)
            await asyncio.wait_for(peers[0].closed.wait(), 0.5)
            assert not errors
            assert caplog.text.count("TLS proxy forced transport abort") == 1

    run(scenario())


@pytest.mark.parametrize("side", ["client", "server"])
def test_eof_or_reset_releases_both_forwarders_without_proxy_close(run, tls_context, side):
    async def scenario():
        async with tls_farm(tls_context) as (connect, peers, errors):
            server, reader, writer = await connect()
            state = camera_tls._proxy_states[server]
            conn = next(iter(state.connections))
            if side == "client":
                writer.close()
                await writer.wait_closed()
            else:
                peers[0].transport.abort()
                assert await reader.read() == b""
            await eventually(lambda: not state.connections, 2.5)
            assert conn.handler.done() and all(task.done() for task in conn.forwarders + conn.writer_waits)
            assert not errors

    run(scenario())


def test_rtsp_digest_and_binary_payload_over_real_proxy(run, tls_context):
    async def scenario():
        async with tls_farm(tls_context) as (connect, peers, errors):
            server, reader, writer = await connect()
            port = server.sockets[0].getsockname()[1]
            proxy = f"rtsp://127.0.0.1:{port}".encode()
            target = f"rtsps://{camera_tls._proxy_states[server].target}".encode()
            for method in (b"DESCRIBE", b"SETUP", b"PLAY"):
                header = b'Authorization: Digest uri="' + proxy + b'/live", response="dont-change"\r\n\r\n'
                data = method + b" " + proxy + b"/live RTSP/1.0\r\n" + header
                peers[0].received.clear()
                writer.write(data)
                await writer.drain()
                await eventually(lambda: len(peers[0].received) > 0)
                assert bytes(peers[0].received) == method + b" " + target + b"/live RTSP/1.0\r\n" + header
            binary = b"$\x00\x00\x10" + bytes(range(16))
            peers[0].received.clear()
            writer.write(binary)
            await writer.drain()
            await eventually(lambda: len(peers[0].received) == len(binary))
            assert peers[0].received == binary
            peers[0].send(binary)
            assert await reader.readexactly(len(binary)) == binary
            await camera.close_tls_proxy(server)
            assert_released(server)
            assert not errors

    run(scenario())


def test_concurrent_close_and_repeated_cancellation_share_cleanup(run, tls_context, monkeypatch):
    monkeypatch.setattr(camera_tls, "_WRITER_CLOSE_GRACE", 0.08)
    monkeypatch.setattr(camera_tls, "_PROXY_CLOSE_TIMEOUT", 0.3)

    async def scenario():
        async with tls_farm(tls_context, silent=True) as (connect, peers, errors):
            server, _, _ = await connect()
            caller = asyncio.create_task(camera.close_tls_proxy(server))
            await asyncio.sleep(0)
            state = camera_tls._proxy_states[server]
            coordinator, deadline = state.close_task, state.deadline
            follower = asyncio.create_task(camera.close_tls_proxy(server))
            for _ in range(3):
                caller.cancel()
                await asyncio.sleep(0.01)
                assert state.close_task is coordinator and state.deadline == deadline
            with pytest.raises(asyncio.CancelledError):
                await caller
            await follower
            assert_released(server)
            assert not errors

    run(scenario())


@pytest.mark.parametrize("error", [ConnectionResetError, RuntimeError])
def test_forwarder_dead_handle_is_runtime_checked(run, error):
    async def scenario():
        reader, writer = Mock(), Mock()
        reader.read = AsyncMock(return_value=b"payload")
        writer.write.side_effect = error("closed handle")
        await camera_tls._forward(reader, writer)
        writer.write.assert_called_once_with(b"payload")

    run(scenario())


def test_cancel_before_handler_starts_and_late_accept_never_open_tls(run, monkeypatch):
    async def scenario():
        class SlotsServer:
            __slots__ = ("__weakref__", "sockets")

            def __init__(self):
                self.sockets = [Mock(getsockname=lambda: ("127.0.0.1", 45678))]

            def close(self):
                pass

            async def wait_closed(self):
                pass

        accepts = []

        async def start_server(callback, *args):
            accepts.append(callback)
            return SlotsServer()

        monkeypatch.setattr(asyncio, "start_server", start_server)
        opening = AsyncMock()
        monkeypatch.setattr(asyncio, "open_connection", opening)
        _, server = await camera.create_tls_proxy("127.0.0.1", 1)
        writer = Mock(wait_closed=AsyncMock())
        accepts[0](Mock(), writer)
        conn = next(iter(camera_tls._proxy_states[server].connections))
        conn.handler.cancel()  # callback registered ownership before task starts
        close = asyncio.create_task(camera.close_tls_proxy(server))
        await asyncio.sleep(0)
        late_writer = Mock(wait_closed=AsyncMock())
        accepts[0](Mock(), late_writer)
        await close
        opening.assert_not_awaited()
        writer.wait_closed.assert_awaited_once()
        late_writer.transport.abort.assert_called_once()
        late_writer.wait_closed.assert_awaited_once()
        assert camera_tls._proxy_states[server].closed

    run(scenario())


def test_deadline_failure_is_sticky_and_keeps_unfinished_transport_owned(run, tls_context, monkeypatch, caplog):
    monkeypatch.setattr(camera_tls, "_WRITER_CLOSE_GRACE", 0.03)
    monkeypatch.setattr(camera_tls, "_PROXY_CLOSE_TIMEOUT", 0.12)

    async def scenario():
        async with tls_farm(tls_context) as (connect, peers, errors):
            server, _, _ = await connect()
            state = camera_tls._proxy_states[server]
            conn = next(iter(state.connections))
            release = asyncio.Event()
            # A fault that even abort cannot complete; never infer closure from
            # is_closing(). The pending waiter must remain strongly owned.
            monkeypatch.setattr(conn.writers[1], "wait_closed", release.wait)
            started = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError):
                await camera.close_tls_proxy(server)
            assert asyncio.get_running_loop().time() - started < 0.5
            assert not state.closed and state.failure and conn in state.connections
            assert any(not task.done() and task in camera_cleanup._owned_tasks for task in conn.writer_waits)
            with pytest.raises(TimeoutError):
                await camera.close_tls_proxy(server)
            assert caplog.text.count("TLS proxy teardown deadline exceeded") == 1
            release.set()
            await asyncio.sleep(0)

    run(scenario())


def test_repeated_cycles_and_separate_proxies_do_not_share_ownership(run, tls_context):
    async def scenario():
        async with tls_farm(tls_context) as (connect, peers, errors):
            other, reader, _ = await connect()
            for _ in range(5):
                server, _, _ = await connect()
                await camera.close_tls_proxy(server)
                assert_released(server)
                assert other.is_serving()
                assert camera_tls._proxy_states[other].connections
            peers[0].send(b"still alive\n")
            assert await reader.readline() == b"still alive\n"
            await camera.close_tls_proxy(other)
            await eventually(lambda: not camera_cleanup._owned_tasks)
            assert not errors

    run(scenario())


def test_tls_connection_error_never_logs_peer_payload_or_credentials(run, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="backend.app.services.camera_tls")

    async def scenario():
        original_open = asyncio.open_connection

        async def opening(*args, **kwargs):
            if "ssl" in kwargs:
                raise OSError('rtsp://bblp:secret@127.0.0.1/live Authorization: Digest response="secret"')
            return await original_open(*args, **kwargs)

        monkeypatch.setattr(asyncio, "open_connection", opening)
        port, server = await camera.create_tls_proxy("127.0.0.1", 1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await camera.close_tls_proxy(server)
            assert_released(server)
            assert "secret" not in caplog.text and "Authorization" not in caplog.text
            assert f"proxy_port={port}" in caplog.text and "target=127.0.0.1:1" in caplog.text
        finally:
            writer.close()
            await writer.wait_closed()

    run(scenario())
