"""Read, check and delete in ONE occupancy of the printer's file lane (G10 C2-3).

``remove_recording_after_attach`` deletes a recording only as the very bytes an
archive holds. The proof and the command must not be two visits to the lane:
between them a recording of the same name could replace the one that was read.
Both real transports are exercised here — FTP against the mock server, the
tunnel with a client double on its real lane.
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.bambu_ftp import DeleteResult, delete_file_verified_async
from backend.app.services.printer_files.tunnel import TunnelTransport

pytestmark = pytest.mark.asyncio

RECORDING = b"the recording an archive holds"


def _is(expected: bytes):
    return lambda data: bool(data) and hashlib.sha256(data).hexdigest() == hashlib.sha256(expected).hexdigest()


class TestOverFtp:
    async def _delete(self, server, *, on_printer=RECORDING, ready=lambda: True, may_run=None):
        server.add_file("timelapse/video.mp4", on_printer)
        return await delete_file_verified_async(
            "127.0.0.1",
            "12345678",
            "/timelapse/video.mp4",
            printer_model="X1C",
            ready=ready,
            verify=_is(RECORDING),
            may_run=may_run,
        )

    async def test_the_expected_bytes_are_deleted(self, patch_ftp_port):
        assert await self._delete(patch_ftp_port) == DeleteResult.DELETED
        assert not patch_ftp_port.file_exists("timelapse/video.mp4")

    async def test_other_bytes_under_the_name_stay(self, patch_ftp_port):
        assert await self._delete(patch_ftp_port, on_printer=b"another recording") == DeleteResult.FAILED
        assert patch_ftp_port.read_file("timelapse/video.mp4") == b"another recording"

    async def test_nothing_is_even_read_when_the_caller_is_not_ready(self, patch_ftp_port):
        asked = []
        result = await self._delete(patch_ftp_port, ready=lambda: asked.append(1) or False)
        assert result == DeleteResult.FAILED and asked == [1]
        assert patch_ftp_port.file_exists("timelapse/video.mp4")

    async def test_the_last_word_is_the_callers(self, patch_ftp_port):
        assert await self._delete(patch_ftp_port, may_run=lambda: False) == DeleteResult.FAILED
        assert patch_ftp_port.file_exists("timelapse/video.mp4")

    async def test_a_file_that_is_not_there_is_not_deleted(self, patch_ftp_port):
        result = await delete_file_verified_async(
            "127.0.0.1",
            "12345678",
            "/timelapse/missing.mp4",
            printer_model="X1C",
            ready=lambda: True,
            verify=_is(RECORDING),
        )
        assert result == DeleteResult.FAILED


class TestOverTheTunnel:
    PATH = "/userdata/media/timelapse/record.mp4"

    def _transport(self, monkeypatch, remote: dict, deleted: list):
        device = SimpleNamespace(id=1, name="fake", ip_address="no-network", access_code="fake")
        transport = TunnelTransport(device)

        async def download_file(name):
            return remote[name]

        async def delete_files(paths):
            deleted.extend(remote.pop(name) for name in paths)

        @asynccontextmanager
        async def client():
            yield SimpleNamespace(download_file=download_file, delete_files=delete_files)

        monkeypatch.setattr(transport, "_client", client)
        return transport

    async def test_the_expected_bytes_are_deleted(self, monkeypatch):
        remote, deleted = {self.PATH: RECORDING}, []
        transport = self._transport(monkeypatch, remote, deleted)
        result = await transport.delete_verified(self.PATH, ready=lambda: True, verify=_is(RECORDING))
        assert result == DeleteResult.DELETED and deleted == [RECORDING]

    async def test_bytes_that_changed_while_the_lane_was_waited_for_stay(self, monkeypatch):
        """The read is on the lane, not before it: what took the name during the wait is what gets compared."""
        remote, deleted = {self.PATH: RECORDING}, []
        transport = self._transport(monkeypatch, remote, deleted)
        waiting, go = asyncio.Event(), asyncio.Event()
        on_lane = transport._on_lane

        async def slow_lane(name, operation):
            waiting.set()
            await go.wait()
            return await on_lane(name, operation)

        monkeypatch.setattr(transport, "_on_lane", slow_lane)
        work = asyncio.create_task(transport.delete_verified(self.PATH, ready=lambda: True, verify=_is(RECORDING)))
        await asyncio.wait_for(waiting.wait(), 5)
        remote[self.PATH] = b"another recording"
        go.set()

        assert await work == DeleteResult.FAILED
        assert deleted == [] and remote[self.PATH] == b"another recording"

    async def test_no_connection_is_opened_when_the_caller_is_not_ready(self, monkeypatch):
        transport = self._transport(monkeypatch, {self.PATH: RECORDING}, [])
        opened = AsyncMock()
        monkeypatch.setattr(transport, "_client", opened)
        assert (
            await transport.delete_verified(self.PATH, ready=lambda: False, verify=_is(RECORDING))
            == DeleteResult.FAILED
        )
        opened.assert_not_called()

    async def test_the_last_word_is_the_callers(self, monkeypatch):
        remote, deleted = {self.PATH: RECORDING}, []
        transport = self._transport(monkeypatch, remote, deleted)
        result = await transport.delete_verified(
            self.PATH, ready=lambda: True, verify=_is(RECORDING), may_run=lambda: False
        )
        assert result == DeleteResult.FAILED and deleted == []
