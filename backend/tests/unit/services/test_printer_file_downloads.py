"""Download-to-file on both transports: streamed, bounded, typed (G10 A, M01/M02).

FTP runs the real client against the mock printer; the tunnel runs the real
client and transport against the fake tunnel. Neither holds a file in memory:
the FTP side writes every recv as it comes, and the tunnel client never asks its
reader for more than one bounded chunk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import threading
from types import SimpleNamespace

import pytest

from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import download_to_path_async
from backend.app.services.bambu_tunnel import client as tunnel_client
from backend.app.services.printer_file_lanes import ftp_key, printer_file_runtime, tunnel_key
from backend.app.services.printer_files.base import (
    TRANSFER_CANCELLED,
    TRANSFER_COMPLETED,
    TRANSFER_INSUFFICIENT_STORAGE,
    TRANSFER_LIMIT_EXCEEDED,
    TRANSFER_NOT_READABLE,
    TRANSFER_PROTOCOL_FAILED,
    TRANSFER_SHORT_READ,
)
from backend.app.services.printer_files.tunnel import TunnelTransport
from backend.tests import tunnel_fixtures
from backend.tests.tunnel_fixtures import FakeTunnelServer
from backend.tests.unit.services import mock_ftp_server as mock

IP, CODE = "127.0.0.1", "12345678"
MIB = 1024 * 1024


def _blob(size: int) -> bytes:
    return os.urandom(size)


async def _until(predicate, seconds=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


@pytest.fixture
def streaming_retr(monkeypatch):
    """RETR that streams the file in 64 KiB pieces, optionally waiting on a gate half way."""
    gate = threading.Event()
    gate.set()
    halfway = threading.Event()

    def _retr(self, argument):
        local = self._local(argument)
        if local is None or not local.is_file():
            self._respond(550, "Failed to open file.")
            self._close_pasv()
            return
        conn = self._open_data()
        if conn is None:
            return
        size = local.stat().st_size
        sent = 0
        try:
            with local.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    conn.sendall(chunk)
                    sent += len(chunk)
                    if sent >= size // 2 and not halfway.is_set():
                        halfway.set()
                        gate.wait(10)
        except OSError:
            self._close_data(conn)
            self._respond(426, "Transfer aborted.")
            return
        self._close_data(conn)
        self._respond(226, "Transfer complete.")

    monkeypatch.setattr(mock._ControlSession, "_cmd_retr", _retr)
    return SimpleNamespace(gate=gate, halfway=halfway)


@pytest.mark.asyncio
class TestFtpDownloadToFile:
    async def test_a_large_file_reaches_the_disk_piece_by_piece(self, patch_ftp_port, streaming_retr, tmp_path):
        server = patch_ftp_port
        data = _blob(24 * MIB)
        server.add_file("timelapse/big.mp4", data)
        seen: list[int] = []
        target = tmp_path / "big.mp4"
        outcome = await download_to_path_async(IP, CODE, "/timelapse/big.mp4", target, progress=seen.append)
        assert outcome.status == TRANSFER_COMPLETED
        assert outcome.check == "size_verified"
        assert outcome.received == len(data)
        assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(data).digest()
        # Written as it came: hundreds of increments, none bigger than one recv.
        assert len(seen) > 300
        assert max(b - a for a, b in zip([0, *seen], seen, strict=False)) <= 64 * 1024

    async def test_a_file_over_the_budget_stops_and_leaves_nothing(self, patch_ftp_port, streaming_retr, tmp_path):
        server = patch_ftp_port
        server.add_file("cache/x.3mf", _blob(2 * MIB))
        target = tmp_path / "x.3mf"
        outcome = await download_to_path_async(IP, CODE, "/cache/x.3mf", target, byte_budget=MIB)
        assert outcome.status == TRANSFER_LIMIT_EXCEEDED
        assert not target.exists()

    async def test_a_volume_under_its_reserve_stops_the_transfer(
        self, patch_ftp_port, streaming_retr, tmp_path, monkeypatch
    ):
        server = patch_ftp_port
        server.add_file("cache/y.3mf", _blob(10 * MIB))
        monkeypatch.setattr(
            bambu_ftp.shutil, "disk_usage", lambda path: SimpleNamespace(total=100 * MIB, used=99 * MIB, free=MIB)
        )
        target = tmp_path / "y.3mf"
        outcome = await download_to_path_async(IP, CODE, "/cache/y.3mf", target, reserve_bytes=512 * MIB)
        assert outcome.status == TRANSFER_INSUFFICIENT_STORAGE
        assert not target.exists()

    async def test_a_short_read_is_not_a_file(self, patch_ftp_port, streaming_retr, tmp_path, monkeypatch):
        server = patch_ftp_port
        server.add_file("cache/z.3mf", _blob(64 * 1024))
        monkeypatch.setattr(mock._ControlSession, "_cmd_size", lambda self, arg: self._respond(213, str(65 * 1024)))
        target = tmp_path / "z.3mf"
        outcome = await download_to_path_async(IP, CODE, "/cache/z.3mf", target)
        assert outcome.status == TRANSFER_SHORT_READ
        assert not target.exists()

    async def test_a_refused_path_is_not_readable_not_missing(self, patch_ftp_port, tmp_path):
        target = tmp_path / "nope.3mf"
        outcome = await download_to_path_async(IP, CODE, "/cache/nope.3mf", target)
        assert outcome.status == TRANSFER_NOT_READABLE
        assert outcome.reason == "550"
        assert not target.exists()

    async def test_a_cancelled_transfer_closes_its_session_and_leaves_no_file(
        self, patch_ftp_port, streaming_retr, tmp_path
    ):
        server = patch_ftp_port
        server.add_file("timelapse/c.mp4", _blob(4 * MIB))
        streaming_retr.gate.clear()
        target = tmp_path / "c.mp4"
        task = asyncio.create_task(download_to_path_async(IP, CODE, "/timelapse/c.mp4", target))
        assert await asyncio.to_thread(streaming_retr.halfway.wait, 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # The worker was told to stop; its socket was closed under the blocked recv.
        await _until(lambda: not printer_file_runtime.lane_busy(ftp_key(IP)))
        streaming_retr.gate.set()
        assert not target.exists()


# ------------------------------------------------------------------- tunnel ---------------------


def _frame(sequence: int, reply: dict, body: bytes = b"", result: int = 0) -> bytes:
    envelope = {"cmdtype": 4, "mtype": tunnel_fixtures.MTYPE_FILE, "sequence": sequence, "result": result}
    envelope["reply"] = reply
    payload = json.dumps(envelope).encode() + tunnel_fixtures.ENVELOPE_SEPARATOR + body
    return tunnel_fixtures.pack_frame(tunnel_fixtures._TYPE_DATA_REPLY, sequence, payload)


@pytest.fixture
async def tunnel():
    server = FakeTunnelServer()
    host, port = await server.start()

    async def connector():
        return await asyncio.open_connection(host, port)

    printer = SimpleNamespace(ip_address="127.0.0.1", access_code=server.access_code)
    transport = TunnelTransport(printer, port=port, connector=connector)
    yield SimpleNamespace(server=server, transport=transport, port=port)
    await server.stop()


@pytest.mark.asyncio
class TestTunnelDownloadToFile:
    async def test_a_large_file_is_streamed_and_its_checksum_verified(self, tunnel, tmp_path, monkeypatch):
        path = "/userdata/media/timelapse/a.mp4"
        data = _blob(12 * MIB)
        tunnel.server.file_bytes[path] = data
        tunnel.server.download_chunk_size = 261120  # what the X2D sends
        tunnel.server.download_md5 = True
        asked: list[int] = []
        original = asyncio.StreamReader.readexactly

        async def recording(self, n):
            asked.append(n)
            return await original(self, n)

        monkeypatch.setattr(asyncio.StreamReader, "readexactly", recording)
        target = tmp_path / "a.mp4"
        outcome = await tunnel.transport.download_to_file(path, target)
        assert outcome.status == TRANSFER_COMPLETED
        assert outcome.check == "checksum_verified"
        assert target.read_bytes() == data
        # Never asked the socket for more than one bounded chunk at a time.
        assert max(asked) <= tunnel_client.READ_CHUNK

    async def test_without_a_checksum_only_the_length_is_claimed(self, tunnel, tmp_path):
        path = "/userdata/media/timelapse/b.mp4"
        tunnel.server.file_bytes[path] = _blob(300_000)
        outcome = await tunnel.transport.download_to_file(path, tmp_path / "b.mp4")
        assert (outcome.status, outcome.check) == (TRANSFER_COMPLETED, "length_verified")

    async def test_a_checksum_that_does_not_match_is_refused(self, tunnel, tmp_path):
        path = "/userdata/media/timelapse/c.mp4"
        tunnel.server.file_bytes[path] = _blob(100_000)
        tunnel.server.download_md5 = "0" * 32
        target = tmp_path / "c.mp4"
        outcome = await tunnel.transport.download_to_file(path, target)
        assert (outcome.status, outcome.reason) == (TRANSFER_PROTOCOL_FAILED, "checksum_mismatch")
        assert not target.exists()

    async def test_the_golden_vector_matches_without_reordering(self):
        # RFC 1321 "abc": the wire digest is the ordinary hex digest, any case (CR3).
        assert hashlib.md5(b"abc", usedforsecurity=False).hexdigest() == "900150983cd24fb0d6963f7d28e17f72"

    @pytest.mark.parametrize(
        "frames, category",
        [
            # A gap: the second frame claims an offset past what arrived.
            (
                lambda s: [
                    _frame(s, {"offset": 0, "total": 20, "size": 10}, b"a" * 10, 1),
                    _frame(s, {"offset": 12, "total": 20, "size": 8}, b"b" * 8),
                ],
                "offset_mismatch",
            ),
            # The total changes mid-transfer.
            (
                lambda s: [
                    _frame(s, {"offset": 0, "total": 20, "size": 10}, b"a" * 10, 1),
                    _frame(s, {"offset": 10, "total": 30, "size": 10}, b"b" * 10),
                ],
                "total_mismatch",
            ),
            # More bytes than the total.
            (lambda s: [_frame(s, {"offset": 0, "total": 5, "size": 10}, b"a" * 10)], "overrun"),
            # The printer says "last" before the total arrived.
            (lambda s: [_frame(s, {"offset": 0, "total": 20, "size": 10}, b"a" * 10, 0)], "short_transfer"),
            # A body that is not the size its envelope states.
            (lambda s: [_frame(s, {"offset": 0, "total": 10, "size": 9}, b"a" * 10)], "offset_mismatch"),
        ],
    )
    async def test_a_broken_stream_is_refused_and_leaves_nothing(self, tunnel, tmp_path, frames, category):
        tunnel.server.download_frames = lambda envelope, sequence: frames(sequence)
        target = tmp_path / "broken.bin"
        outcome = await tunnel.transport.download_to_file("/userdata/x.bin", target)
        assert (outcome.status, outcome.reason) == (TRANSFER_PROTOCOL_FAILED, category)
        assert not target.exists()

    async def test_an_oversized_frame_is_refused_before_it_is_read(self, tunnel, tmp_path):
        def oversized(envelope, sequence):
            claimed = tunnel_client.MAX_FRAME_PAYLOAD + 1
            header = struct.pack("<IIII", claimed, tunnel_fixtures._TYPE_DATA_REPLY, sequence, 0)
            return [header + b"{"]  # never sends the rest

        tunnel.server.download_frames = oversized
        outcome = await tunnel.transport.download_to_file("/userdata/x.bin", tmp_path / "x.bin")
        assert (outcome.status, outcome.reason) == (TRANSFER_PROTOCOL_FAILED, "oversized_frame")

    async def test_endless_foreign_frames_are_bounded(self, tunnel, tmp_path):
        def flood(envelope, sequence):
            other = sequence + 1000
            return [_frame(other, {"pad": "x"}) for _ in range(tunnel_client.MAX_PARKED_FRAMES + 1)]

        tunnel.server.download_frames = flood
        outcome = await tunnel.transport.download_to_file("/userdata/x.bin", tmp_path / "x.bin")
        assert (outcome.status, outcome.reason) == (TRANSFER_PROTOCOL_FAILED, "too_many_parked")

    async def test_an_envelope_that_never_closes_is_refused_quickly(self, tunnel, tmp_path):
        def unbounded(envelope, sequence):
            payload = b'{"sequence": ' + str(sequence).encode() + b', "x": "' + b"x" * (70 * 1024)
            return [tunnel_fixtures.pack_frame(tunnel_fixtures._TYPE_DATA_REPLY, sequence, payload)]

        tunnel.server.download_frames = unbounded
        outcome = await tunnel.transport.download_to_file("/userdata/x.bin", tmp_path / "x.bin")
        assert (outcome.status, outcome.reason) == (TRANSFER_PROTOCOL_FAILED, "envelope_unbounded")

    async def test_a_cancelled_download_leaves_no_file_and_frees_the_lane(self, tunnel, tmp_path):
        path = "/userdata/media/timelapse/d.mp4"
        tunnel.server.file_bytes[path] = _blob(2 * MIB)
        tunnel.server.download_chunk_size = 64 * 1024
        tunnel.server.download_frame_delay = 0.02
        target = tmp_path / "d.mp4"
        task = asyncio.create_task(tunnel.transport.download_to_file(path, target))
        await _until(lambda: target.exists() and target.stat().st_size > 0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _until(lambda: not printer_file_runtime.lane_busy(tunnel_key("127.0.0.1", tunnel.port)))
        await _until(lambda: not target.exists())

    async def test_a_refused_download_is_not_readable(self, tunnel, tmp_path):
        outcome = await tunnel.transport.download_to_file("/userdata/missing.bin", tmp_path / "m.bin")
        assert outcome.status == TRANSFER_NOT_READABLE
        assert outcome.reason == "result=14"


def test_the_cancelled_status_is_part_of_the_contract():
    assert TRANSFER_CANCELLED == "cancelled"
