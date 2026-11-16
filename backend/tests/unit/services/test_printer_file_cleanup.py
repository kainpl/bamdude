"""Cleanup stays owned until it has really ended (G10 A, Codex review A1/A2).

A1 — a tunnel operation's socket close is part of the operation: the caller's
cancel, a restore's cancel and any number after them may turn the graceful close
into an abort, but never end the operation — and so release its lane or report
a drain — before the transport has let go of the socket.

A2 — a download's local file is owned from the open to its removal: a cancel
during the open, the writes, the flush or the cleanup itself never leaves a file
or an open descriptor behind once the owner is gone, and an error nobody
expected is raised again only after the file is gone.

Each runtime here is a fresh ``PrinterFileRuntime`` patched into the transport,
so a quiesce in one test never closes admission for another.
"""

from __future__ import annotations

import asyncio
import ftplib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import BambuFTPClient, download_to_path_async
from backend.app.services.printer_file_lanes import LaneRefused, PrinterFileRuntime, ftp_key, tunnel_key
from backend.app.services.printer_files import tunnel as tunnel_transport
from backend.app.services.printer_files.base import TRANSFER_CANCELLED
from backend.app.services.printer_files.staging import StagingNotRemoved
from backend.app.services.printer_files.tunnel import TunnelTransport
from backend.tests.tunnel_fixtures import FakeTunnelServer

IP, CODE = "127.0.0.1", "12345678"
MIB = 1024 * 1024


async def _until(predicate, seconds: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ A1: tunnel close ---------------


class HeldWriter:
    """A real StreamWriter whose graceful close never completes on its own.

    ``abortable`` gives it a transport whose ``abort`` is the real one; without
    it the client has nothing to abort and can only wait for ``release``.
    """

    def __init__(self, writer: asyncio.StreamWriter, abortable: bool):
        self._writer = writer
        self._abortable = abortable
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.aborted = False
        self.closed = False

    def write(self, data) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self.closing.set()

    async def wait_closed(self) -> None:
        await self.release.wait()
        self._writer.close()
        await self._writer.wait_closed()
        self.closed = True

    @property
    def transport(self):
        return self if self._abortable else None

    def abort(self) -> None:
        self.aborted = True
        self._writer.transport.abort()
        self.release.set()


@pytest.fixture
async def held_tunnel(monkeypatch, tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    runtime = PrinterFileRuntime()
    monkeypatch.setattr(tunnel_transport, "printer_file_runtime", runtime)
    writers: list[HeldWriter] = []
    state = SimpleNamespace(abortable=False)

    async def connector():
        reader, writer = await asyncio.open_connection(host, port)
        held = HeldWriter(writer, state.abortable)
        writers.append(held)
        return reader, held

    printer = SimpleNamespace(ip_address=IP, access_code=server.access_code)
    transport = TunnelTransport(printer, port=port, connector=connector)
    server.file_bytes["/userdata/a.3mf"] = b"z" * 4096
    server.file_bytes["/userdata/a.3mf#Metadata/plate_1.png"] = b"\x89PNG" + b"p" * 100
    upload = tmp_path / "up.3mf"
    upload.write_bytes(b"u" * 2048)
    yield SimpleNamespace(
        server=server,
        transport=transport,
        runtime=runtime,
        writers=writers,
        state=state,
        key=tunnel_key(IP, port),
        upload=upload,
        tmp=tmp_path,
    )
    for held in writers:
        held.release.set()
    await asyncio.sleep(0)
    await server.stop()


def _operation(name: str, h):
    if name == "list":
        return h.transport.list_files("/")
    if name == "read":
        return h.transport.read_bytes("/userdata/a.3mf")
    if name == "sub_file":
        return h.transport.read_bytes("/userdata/a.3mf#Metadata/plate_1.png")
    if name == "upload":
        return h.transport.upload(h.upload, "up.3mf")
    if name == "delete":
        return h.transport.delete("/userdata/a.3mf")
    if name == "download":
        return h.transport.download_to_file("/userdata/a.3mf", h.tmp / "a.3mf")
    raise AssertionError(name)


OPERATIONS = ["list", "read", "sub_file", "upload", "delete", "download"]


@pytest.mark.asyncio
class TestTunnelCloseStaysOwned:
    @pytest.mark.parametrize("name", OPERATIONS)
    async def test_repeated_cancel_never_drains_over_an_unfinished_close(self, held_tunnel, name):
        h = held_tunnel
        caller = asyncio.create_task(_operation(name, h))
        await _until(lambda: h.writers and h.writers[0].closing.is_set())
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert h.runtime.in_flight() == 1
        # A restore: its cancel lands in the close the caller's cancel left running.
        assert await h.runtime.quiesce(0.05) is False
        # And again — no number of cancels turns an unfinished close into a drain.
        assert await h.runtime.quiesce(0.05) is False
        assert h.runtime.in_flight() == 1
        assert h.runtime.lane_busy(h.key)
        assert h.writers[0].closed is False
        h.writers[0].release.set()
        assert await h.runtime.quiesce(5) is True
        assert h.writers[0].closed is True
        assert not h.runtime.lane_busy(h.key)
        h.runtime.reopen()

    @pytest.mark.parametrize("name", OPERATIONS)
    async def test_the_next_operation_waits_for_the_close(self, held_tunnel, name):
        h = held_tunnel
        caller = asyncio.create_task(_operation(name, h))
        await _until(lambda: h.writers and h.writers[0].closing.is_set())
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)

        async def probe():
            return "ran"

        with pytest.raises(LaneRefused) as refused:
            await h.runtime.run_task(h.key, "probe", probe, admission_timeout=0.1)
        assert refused.value.reason == "busy"
        h.writers[0].release.set()
        assert await h.runtime.run_task(h.key, "probe", probe, admission_timeout=5) == "ran"

    async def test_a_cancel_turns_the_close_into_an_abort_and_waits_for_it(self, held_tunnel):
        h = held_tunnel
        h.state.abortable = True
        caller = asyncio.create_task(_operation("list", h))
        await _until(lambda: h.writers and h.writers[0].closing.is_set())
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert await h.runtime.quiesce(5) is True
        assert h.writers[0].aborted is True
        assert h.writers[0].closed is True

    async def test_a_close_that_runs_out_of_grace_is_aborted_without_any_cancel(self, held_tunnel, monkeypatch):
        h = held_tunnel
        h.state.abortable = True
        original = TunnelTransport._client

        def quick(self):
            client = original(self)
            client._close_grace = 0.05
            return client

        monkeypatch.setattr(TunnelTransport, "_client", quick)
        assert [f.name for f in await h.transport.list_files("/")] == []
        assert h.writers[0].aborted is True
        assert h.runtime.in_flight() == 0

    async def test_the_cancel_that_reached_the_close_is_still_raised(self, held_tunnel):
        """Not swallowed: the operation ends cancelled once its socket is gone."""
        h = held_tunnel
        h.state.abortable = True

        async def operation():
            async with h.transport._client():
                await asyncio.Event().wait()

        task = asyncio.create_task(h.runtime.run_task(h.key, "probe", operation))
        await _until(lambda: h.writers)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _until(lambda: h.runtime.in_flight() == 0)
        assert h.writers[0].closed is True


# --------------------------------------------------------- A2: the tunnel's staging file -----------


class GatedFile:
    """The real file, with one stage (write / flush / close) held on a thread gate."""

    def __init__(self, real, stage: str | None, entered: threading.Event, gate: threading.Event):
        self._real = real
        self._stage = stage
        self._entered = entered
        self._gate = gate

    def _hold(self, stage: str) -> None:
        if stage == self._stage:
            self._entered.set()
            assert self._gate.wait(10)

    def write(self, data):
        self._hold("write")
        return self._real.write(data)

    def flush(self):
        self._hold("flush")
        return self._real.flush()

    def close(self):
        self._hold("close")
        return self._real.close()

    @property
    def closed(self):
        return self._real.closed


@pytest.fixture
async def staging(monkeypatch, tmp_path):
    server = FakeTunnelServer()
    host, port = await server.start()
    runtime = PrinterFileRuntime()
    monkeypatch.setattr(tunnel_transport, "printer_file_runtime", runtime)

    async def connector():
        return await asyncio.open_connection(host, port)

    printer = SimpleNamespace(ip_address=IP, access_code=server.access_code)
    transport = TunnelTransport(printer, port=port, connector=connector)
    server.file_bytes["/userdata/v.mp4"] = b"v" * (256 * 1024)
    server.download_chunk_size = 64 * 1024
    files: list = []
    state = SimpleNamespace(stage=None, entered=threading.Event(), gate=threading.Event(), open_gate=None)
    real_open = open

    def gated_open(path, mode="r", *args, **kwargs):
        if state.open_gate is not None:
            state.entered.set()
            assert state.open_gate.wait(10)
        real = real_open(path, mode, *args, **kwargs)
        files.append(real)
        return GatedFile(real, state.stage, state.entered, state.gate)

    monkeypatch.setattr(tunnel_transport, "open", gated_open, raising=False)
    yield SimpleNamespace(
        transport=transport,
        runtime=runtime,
        state=state,
        files=files,
        target=tmp_path / "v.mp4",
        key=tunnel_key(IP, port),
    )
    state.gate.set()
    if state.open_gate is not None:
        state.open_gate.set()
    await server.stop()


@pytest.mark.asyncio
class TestTunnelStagingStaysOwned:
    @pytest.mark.parametrize("stage", ["open", "write", "flush", "close"])
    async def test_a_cancel_and_a_restore_at_any_stage_leave_nothing(self, staging, stage):
        s = staging
        if stage == "open":
            s.state.open_gate = threading.Event()
        else:
            s.state.stage = stage
        caller = asyncio.create_task(s.transport.download_to_file("/userdata/v.mp4", s.target))
        assert await asyncio.to_thread(s.state.entered.wait, 10)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        # The stage is still running on its thread: the operation owns it.
        assert s.runtime.in_flight() == 1
        assert await s.runtime.quiesce(0.05) is False
        assert await s.runtime.quiesce(0.05) is False
        (s.state.open_gate or s.state.gate).set()
        assert await s.runtime.quiesce(10) is True
        assert not s.target.exists()
        assert s.files and all(f.closed for f in s.files)

    async def test_a_cancel_during_the_cleanup_does_not_cut_it_short(self, staging, monkeypatch):
        s = staging
        s.state.stage = "write"
        entered, gate = threading.Event(), threading.Event()
        real_discard = tunnel_transport._discard

        def held_discard(handle, destination):
            entered.set()
            assert gate.wait(10)
            real_discard(handle, destination)

        monkeypatch.setattr(tunnel_transport, "_discard", held_discard)
        caller = asyncio.create_task(s.transport.download_to_file("/userdata/v.mp4", s.target))
        assert await asyncio.to_thread(s.state.entered.wait, 10)
        caller.cancel()
        s.state.gate.set()  # the write ends; the cancel takes the operation into its cleanup
        assert await asyncio.to_thread(entered.wait, 10)
        await asyncio.gather(caller, return_exceptions=True)
        # The cleanup is running: two more cancels may not stop it half way.
        assert await s.runtime.quiesce(0.05) is False
        assert await s.runtime.quiesce(0.05) is False
        assert s.target.exists()
        gate.set()
        assert await s.runtime.quiesce(10) is True
        assert not s.target.exists()
        assert all(f.closed for f in s.files)

    async def test_a_file_that_cannot_be_removed_is_not_reported_as_an_outcome(self, staging, monkeypatch):
        s = staging

        def refuse(path):
            raise StagingNotRemoved("partial file v.mp4 was not removed")

        monkeypatch.setattr(tunnel_transport, "discard_staging", refuse)
        # The printer refuses the path after the file was opened; the file then stays.
        with pytest.raises(StagingNotRemoved):
            await s.transport.download_to_file("/userdata/missing.bin", s.target)
        assert s.runtime.in_flight() == 0


# ------------------------------------------------------------- A2: the FTP staging file ---------------


class FakeFtp:
    """An ftplib stand-in: RETR sends one chunk, then raises what the test asks for."""

    def __init__(self, error: BaseException | None, *, wait: threading.Event | None = None):
        self.error = error
        self.wait = wait
        self.sent = threading.Event()

    def retrbinary(self, command, callback, blocksize=8192, rest=None):
        callback(b"x" * 1024)
        self.sent.set()
        if self.wait is not None:
            assert self.wait.wait(10)
        if self.error is not None:
            raise self.error
        return "226 Transfer complete."

    def size(self, path):
        return 1024

    def voidcmd(self, command):
        return "200"

    def quit(self):
        return "221"

    def close(self):
        pass


# Captured on loopback FTPS (Codex review A2): what ftplib's unwrap raised once an
# SSLSocket had been shut down under its blocked recv.
CAPTURED = "No SSL wrapper around <ssl.SSLSocket fd=1>"


@pytest.mark.asyncio
class TestFtpStagingStaysOwned:
    @staticmethod
    def _client(error):
        client = BambuFTPClient(IP, CODE)
        client._ftp = FakeFtp(error)
        return client

    async def test_an_unexpected_error_is_raised_after_the_file_is_gone(self, tmp_path):
        target = tmp_path / "f.mp4"
        with pytest.raises(ValueError, match="No SSL wrapper"):
            self._client(ValueError(CAPTURED)).fetch_to_file("/f.mp4", target)
        assert not target.exists()

    async def test_the_legacy_download_discards_on_an_unexpected_error_too(self, tmp_path):
        target = tmp_path / "f.3mf"
        with pytest.raises(ValueError, match="No SSL wrapper"):
            self._client(ValueError(CAPTURED)).download_to_file("/f.3mf", target)
        assert not target.exists()

    async def test_a_550_leaves_no_empty_file_behind(self, tmp_path):
        target = tmp_path / "g.3mf"
        with pytest.raises(bambu_ftp.FileNotOnPrinterError):
            self._client(ftplib.error_perm("550 Failed to open file.")).download_to_file("/g.3mf", target)
        assert not target.exists()

    async def test_a_file_that_cannot_be_removed_raises_instead_of_an_outcome(self, tmp_path, monkeypatch):
        target = tmp_path / "h.mp4"

        def refuse(path: Path):
            raise StagingNotRemoved("partial file h.mp4 was not removed")

        monkeypatch.setattr(bambu_ftp, "discard_staging", refuse)
        with pytest.raises(StagingNotRemoved):
            self._client(OSError("reset")).fetch_to_file("/h.mp4", target)

    async def test_a_restore_that_interrupts_the_transfer_gets_a_typed_cancel(self, tmp_path, monkeypatch):
        """The error the cancel's abort causes is answered as ``cancelled`` — the file already gone."""
        runtime = PrinterFileRuntime()
        monkeypatch.setattr(bambu_ftp, "printer_file_runtime", runtime)
        release = threading.Event()
        fake = FakeFtp(ValueError(CAPTURED), wait=release)

        def connect(self):
            self._ftp = fake
            return True

        monkeypatch.setattr(BambuFTPClient, "connect", connect)
        target = tmp_path / "r.mp4"
        caller = asyncio.create_task(download_to_path_async(IP, CODE, "/r.mp4", target))
        assert await asyncio.to_thread(fake.sent.wait, 10)
        restore = asyncio.create_task(runtime.quiesce(10))
        await _until(lambda: runtime.closed)
        release.set()
        outcome = await caller
        assert (outcome.status, outcome.reason) == (TRANSFER_CANCELLED, "restoring")
        assert await restore is True
        assert not target.exists()
        assert not runtime.lane_busy(ftp_key(IP))

    async def test_without_a_cancel_the_unexpected_error_reaches_the_caller(self, tmp_path, monkeypatch):
        runtime = PrinterFileRuntime()
        monkeypatch.setattr(bambu_ftp, "printer_file_runtime", runtime)
        fake = FakeFtp(ValueError(CAPTURED))

        def connect(self):
            self._ftp = fake
            return True

        monkeypatch.setattr(BambuFTPClient, "connect", connect)
        target = tmp_path / "u.mp4"
        with pytest.raises(ValueError, match="No SSL wrapper"):
            await download_to_path_async(IP, CODE, "/u.mp4", target)
        assert not target.exists()
        assert runtime.in_flight() == 0
