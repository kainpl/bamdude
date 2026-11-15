"""One 3MF recovery walk: what it proves and what it never fetches twice (G10 A, M05).

The FTP client is a fake over a set of remote paths: some are there, some
answer 550, and one starts its transfer and fails. The walk runs the real
``archive_download`` stages over the real lane.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import archive_download
from backend.app.services.archive_download_retry import ArchiveDownloadRetryService


class WalkClient:
    present: set[str] = set()
    broken: set[str] = set()
    fetched: list[str] = []
    sessions = 0

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        WalkClient.sessions += 1
        return True

    def download_to_file(self, remote_path, local_path):
        from backend.app.services.bambu_ftp import FileNotOnPrinterError

        WalkClient.fetched.append(remote_path)
        if remote_path in WalkClient.broken:
            return False  # the transfer started and failed (short read, dropped data channel)
        if remote_path not in WalkClient.present:
            raise FileNotOnPrinterError(remote_path)
        Path(local_path).write_bytes(b"PK")
        return True

    def disconnect(self):
        pass


@pytest.fixture
def walk(monkeypatch):
    import backend.app.services.bambu_ftp as bambu_ftp

    WalkClient.present, WalkClient.broken, WalkClient.fetched, WalkClient.sessions = set(), set(), [], 0
    monkeypatch.setattr(bambu_ftp, "BambuFTPClient", WalkClient)
    monkeypatch.setattr(archive_download, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))
    monkeypatch.setattr(archive_download, "_try_internal_storage", AsyncMock(return_value=None))
    return WalkClient


def _printer():
    return SimpleNamespace(id=7, ip_address="10.0.0.9", access_code="12345678", model="X1C", name="X1C")


@pytest.mark.asyncio
async def test_a_failed_transfer_stops_the_walk_and_the_fuzzy_stage_does_not_fetch_it_again(
    walk, tmp_path, monkeypatch
):
    walk.broken = {"/lamp.gcode.3mf"}

    async def listed(ip, code, directory, **kwargs):
        # The fuzzy stage would find the very file whose transfer just failed.
        return [{"name": "lamp.gcode.3mf"}] if directory == "/" else []

    monkeypatch.setattr(archive_download, "list_files_async", listed)
    result = await archive_download.try_download_3mf(_printer(), "lamp", None, tmp_path)

    assert result is None
    # Stage one stopped at the broken transfer instead of walking every other
    # candidate on a session no longer known to be healthy, and the fuzzy stage
    # left the path this walk had already tried alone.
    assert walk.fetched == ["/lamp.gcode.3mf"]


@pytest.mark.asyncio
async def test_550_candidates_are_walked_on_one_session_and_the_real_name_returned(walk, tmp_path, monkeypatch):
    walk.present = {"/cache/lamp.3mf"}
    monkeypatch.setattr(archive_download, "list_files_async", AsyncMock(return_value=[]))
    result = await archive_download.try_download_3mf(_printer(), "lamp", None, tmp_path)

    assert result is not None
    _temp_path, name = result
    assert name == "lamp.3mf"
    assert result.storage == "external"
    assert walk.sessions == 1


@pytest.mark.asyncio
async def test_internal_storage_is_named_as_the_source(tmp_path, monkeypatch):
    from backend.app.services.printer_files.base import TransferOutcome

    async def download_to_file(path, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"PK")
        return TransferOutcome("completed", 2, "checksum_verified")

    entries = [SimpleNamespace(name="lamp.gcode.3mf", path="/lamp.gcode.3mf")]
    transport = SimpleNamespace(list_files=AsyncMock(return_value=entries), download_to_file=download_to_file)
    monkeypatch.setattr(
        "backend.app.utils.printer_storage.storage_capability_for", lambda *a: {"can_browse_internal": True}
    )
    monkeypatch.setattr("backend.app.services.printer_files.factory.transport_for", lambda *a: transport)
    result = await archive_download._try_internal_storage(_printer(), ["lamp.gcode.3mf"], tmp_path)
    assert result is not None and result.storage == "internal" and result[1] == "lamp.gcode.3mf"


@pytest.mark.asyncio
async def test_an_internal_copy_that_fails_its_check_is_not_recovered(tmp_path, monkeypatch):
    from backend.app.services.printer_files.base import TransferOutcome

    entries = [SimpleNamespace(name="lamp.gcode.3mf", path="/lamp.gcode.3mf")]
    transport = SimpleNamespace(
        list_files=AsyncMock(return_value=entries),
        download_to_file=AsyncMock(return_value=TransferOutcome("protocol_failed", 10, reason="checksum_mismatch")),
    )
    monkeypatch.setattr(
        "backend.app.utils.printer_storage.storage_capability_for", lambda *a: {"can_browse_internal": True}
    )
    monkeypatch.setattr("backend.app.services.printer_files.factory.transport_for", lambda *a: transport)
    assert await archive_download._try_internal_storage(_printer(), ["lamp.gcode.3mf"], tmp_path) is None


@pytest.mark.asyncio
async def test_only_the_first_claimer_of_an_archive_owns_its_download():
    retry = ArchiveDownloadRetryService()
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[bool] = []

    async def first():
        async with retry.claim(42) as mine:
            seen.append(mine)
            entered.set()
            await release.wait()

    holder = asyncio.create_task(first())
    await entered.wait()
    async with retry.claim(42) as mine:
        seen.append(mine)
    release.set()
    await holder
    assert seen == [True, False]
    # The loser released nothing: the claim ended with its owner.
    assert not retry.in_progress(42)
