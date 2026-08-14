"""The tunnel client against a fake printer.

Every test here corresponds to something the live X2D actually did — the bare
control ack, the late session reply, the non-zero flag byte — rather than to
the protocol as it would be convenient for us.
"""

import asyncio

import pytest

from backend.app.services.bambu_tunnel.client import BambuTunnelClient, TunnelError
from backend.app.services.bambu_tunnel.codec import TYPE_CONTROL_REQUEST, pack_frame
from backend.tests.tunnel_fixtures import FakeTunnelServer, listing_entry


async def _connected(server: FakeTunnelServer, host: str, port: int) -> BambuTunnelClient:
    async def connector():
        return await asyncio.open_connection(host, port)

    client = BambuTunnelClient("127.0.0.1", server.access_code, port=port, timeout=2.0, connector=connector)
    await client.connect()
    return client


@pytest.mark.asyncio
async def test_the_fake_hangs_up_on_a_wrong_access_code():
    server = FakeTunnelServer(access_code="12345678")
    host, port = await server.start()
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(pack_frame(TYPE_CONTROL_REQUEST, 0, b"bblp" + b"\x00" * 4 + b"99999999"))
        await writer.drain()
        assert await reader.read(1) == b""  # closed, no reply
        writer.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_handshake_returns_the_printers_own_storage_names():
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        reply = await client.handshake()
        assert reply["storage"] == ["emmc", "udisk"]
        assert reply["upload_storage"] == ["emmc", "udisk"]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_bare_control_ack_does_not_derail_the_client():
    """The live printer answers the session frame with four zero bytes and no
    JSON. A client that assumes every frame carries an envelope dies here."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        assert (await client.handshake())["api_version"] == 3
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_listing_survives_replies_arriving_out_of_order():
    """The printer answered the session frame after the handshake request had
    already gone out. Correlation is by sequence, never by arrival."""
    server = FakeTunnelServer()
    server.answer_out_of_order = True
    server.files = [listing_entry("a.gcode.3mf"), listing_entry("b.gcode.3mf")]
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        await client.handshake()
        files = await client.list_files("internal")
        assert [f["name"] for f in files] == ["a.gcode.3mf", "b.gcode.3mf"]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_listing_sends_exactly_the_request_studio_sends():
    """No invented paging cursor: the captured request has api_version, notify,
    type and (for internal) storage — and nothing else."""
    server = FakeTunnelServer()
    server.files = [listing_entry("a.gcode.3mf")]
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        await client.list_files("internal")
        listing = [r for r in server.requests if r.get("cmdtype") == 1][0]
        assert set(listing["req"]) == {"api_version", "notify", "type", "storage"}
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_external_medium_omits_the_storage_key_entirely():
    """Four spellings of two storages, and external is the ABSENCE of the key."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        await client.list_files(None)
        listing = [r for r in server.requests if r.get("cmdtype") == 1][0]
        assert "storage" not in listing["req"]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_truncated_listing_warns_instead_of_pretending(caplog):
    """A non-zero start means there is more and we do not know how to ask for
    it. Say so loudly rather than silently returning a partial list."""
    server = FakeTunnelServer()
    server.report_start = 2
    server.files = [listing_entry("a.gcode.3mf")]
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        with caplog.at_level("WARNING"):
            files = await client.list_files("internal")
        assert len(files) == 1
        assert "truncated" in caplog.text
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_an_old_printer_answering_with_timelapses_is_refused():
    """⚠️ Old firmware answers ANY catalogue request with timelapses.
    BambuStudio detects it by the empty ``path`` and errors out — its own
    comment says "Fix old printer that always return timelapses". Without the
    check a browser shows recordings labelled as models."""
    server = FakeTunnelServer()
    server.files = [{"name": "clip.mp4", "path": "", "size": 10, "time": 1786638756}]
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        with pytest.raises(TunnelError):
            await client.list_files("internal", file_type="model")
        # …and the timelapse catalogue itself is still perfectly legal.
        assert await client.list_files("internal", file_type="timelapse")
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_reading_a_file_returns_the_body_after_the_envelope():
    server = FakeTunnelServer()
    path = "/userdata/model/history/a.gcode.3mf"
    server.file_bytes[path] = b"PK\x03\x04payload"
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        assert await client.read_file(path, "internal") == b"PK\x03\x04payload"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_delete_sends_the_paths():
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        await client.delete_files(["/userdata/model/history/a.gcode.3mf"])
        assert server.deleted == ["/userdata/model/history/a.gcode.3mf"]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_non_zero_result_raises_with_the_value():
    server = FakeTunnelServer()
    server.fail_next_with = 5
    host, port = await server.start()
    try:
        client = await _connected(server, host, port)
        with pytest.raises(TunnelError) as excinfo:
            await client.handshake()
        assert excinfo.value.result == 5
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_dead_peer_does_not_hang_forever():
    """P1S and A1 mini accept the connection on 6000 and answer nothing at all —
    that is the camera daemon, not a tunnel. The client must give up on a
    timer rather than wait on a peer that will never speak.

    ⚠️ The silent peer tracks its writers because Python 3.12's
    ``Server.wait_closed()`` waits for every client transport to close; a
    handler that simply returns leaks one and hangs the teardown.
    """
    peers: list[asyncio.StreamWriter] = []

    async def say_nothing(_reader, writer):
        peers.append(writer)
        await asyncio.sleep(30)

    silent = await asyncio.start_server(say_nothing, "127.0.0.1", 0)
    host, port = silent.sockets[0].getsockname()[:2]
    try:

        async def connector():
            return await asyncio.open_connection(host, port)

        client = BambuTunnelClient("127.0.0.1", "12345678", port=port, timeout=0.3, connector=connector)
        await client.connect()
        with pytest.raises(TunnelError):
            await client.handshake()
        await client.close()
    finally:
        for writer in peers:
            writer.close()
        silent.close()
        await silent.wait_closed()
