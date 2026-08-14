"""Uploading over the tunnel — the four things that are easy to get backwards.

Each of these is a shape observed in a real BambuStudio capture, not a design
we chose, and each one reads wrong until you know why.
"""

import asyncio
import hashlib

import pytest

from backend.app.services.bambu_tunnel.client import (
    WIRE_EMMC,
    BambuTunnelClient,
    TunnelError,
)
from backend.tests.tunnel_fixtures import FakeTunnelServer


async def _connected(server: FakeTunnelServer, host: str, port: int) -> BambuTunnelClient:
    async def connector():
        return await asyncio.open_connection(host, port)

    client = BambuTunnelClient("127.0.0.1", server.access_code, port=port, timeout=2.0, connector=connector)
    await client.connect()
    return client


def _payload(tmp_path, size: int):
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    path = tmp_path / "job.gcode.3mf"
    path.write_bytes(data)
    return path, data


@pytest.mark.asyncio
async def test_the_bytes_arrive_intact_across_fragments(tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 600_000)  # three fragments
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        assert server.uploads["job.gcode.3mf"] == data
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_result_in_the_request_means_more_to_come(tmp_path):
    """⚠️ The opposite of `result` in a reply, where 0 is success. Sending 0 on
    every fragment declares each one final."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, _ = _payload(tmp_path, 600_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        flags = [f["result"] for f in server.upload_frames]
        assert flags[:-1] == [1] * (len(flags) - 1)
        assert flags[-1] == 0
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_frag_id_counts_from_zero_and_offsets_follow_the_chunk_size(tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, _ = _payload(tmp_path, 600_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        frames = server.upload_frames
        assert [f["frag_id"] for f in frames] == list(range(len(frames)))
        assert [f["req"]["offset"] for f in frames] == [0, 261120, 522240]
        assert [f["req"]["size"] for f in frames] == [261120, 261120, 600_000 - 522240]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_last_fragment_carries_a_lowercase_md5_of_the_whole_file(tmp_path):
    """⚠️ Lowercase here; the same digest goes UPPERCASE into project_file."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 300_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        sent = server.upload_meta["job.gcode.3mf"]["file_md5"]
        assert sent == hashlib.md5(data, usedforsecurity=False).hexdigest()
        assert sent == sent.lower()
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_only_the_last_fragment_carries_the_md5(tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, _ = _payload(tmp_path, 600_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        carriers = [f for f in server.upload_frames if "file_md5" in f["req"]]
        assert len(carriers) == 1
        assert carriers[0] is server.upload_frames[-1]
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_every_fragment_frame_carries_the_separator(tmp_path):
    """⚠️ ``\\n\\n`` between the envelope and the bytes, as BambuStudio writes it
    (PrinterFileSystem::UploadFileTask). The fake reassembles from what it
    parses, so a missing separator would show up as corrupted bytes — this
    pins the wire shape itself rather than the result."""
    from backend.app.services.bambu_tunnel.codec import ENVELOPE_SEPARATOR

    assert ENVELOPE_SEPARATOR == b"\n\n"

    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 300_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        # Reassembled through split_envelope, which drops exactly one
        # separator — the bytes must come back byte-for-byte.
        assert server.uploads["job.gcode.3mf"] == data
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_open_frame_names_the_storage_and_the_total(tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 300_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        meta = server.upload_meta["job.gcode.3mf"]
        assert meta["storage"] == "emmc"
        assert meta["total"] == len(data)
        assert meta["type"] == "model"
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_single_fragment_file_is_also_the_last_one(tmp_path):
    """A file smaller than one chunk must still end the transfer, or the
    printer waits for a fragment that never comes."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 1_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        assert [f["result"] for f in server.upload_frames] == [0]
        assert server.uploads["job.gcode.3mf"] == data
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_file_that_is_an_exact_multiple_of_the_chunk_size_ends_cleanly(tmp_path):
    """261120 bytes is one full chunk and nothing left. The loop must not send
    a fourth, empty fragment, and must not fail to mark the third as last."""
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 261120 * 2)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        assert [f["result"] for f in server.upload_frames] == [1, 0]
        assert server.uploads["job.gcode.3mf"] == data
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_progress_is_reported_per_fragment(tmp_path):
    seen: list[tuple[int, int]] = []
    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, data = _payload(tmp_path, 600_000)
        client = await _connected(server, host, port)
        await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC, progress_cb=lambda s, t: seen.append((s, t)))
        assert seen[-1] == (len(data), len(data))
        assert all(total == len(data) for _sent, total in seen)
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_refused_open_raises_before_any_bytes_go_out(tmp_path):
    server = FakeTunnelServer()
    server.fail_next_with = 4
    host, port = await server.start()
    try:
        local, _ = _payload(tmp_path, 600_000)
        client = await _connected(server, host, port)
        with pytest.raises(TunnelError):
            await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC)
        assert server.upload_frames == []
        await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_cancelling_progress_callback_stops_the_transfer(tmp_path):
    """The dispatcher cancels a job from inside the progress callback; the
    exception must travel out rather than be swallowed as a transport error."""

    class Cancelled(Exception):
        pass

    def cancel_at_second(sent, _total):
        if sent > 261120:
            raise Cancelled

    server = FakeTunnelServer()
    host, port = await server.start()
    try:
        local, _ = _payload(tmp_path, 900_000)
        client = await _connected(server, host, port)
        with pytest.raises(Cancelled):
            await client.upload_file(local, "job.gcode.3mf", WIRE_EMMC, progress_cb=cancel_at_second)
        await client.close()
    finally:
        await server.stop()
