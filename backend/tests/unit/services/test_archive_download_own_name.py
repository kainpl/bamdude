"""The directory search of a 3MF recovery takes the print's OWN file only (upstream bb0bf03f6).

When no direct path downloads, the walk lists the printer's folders. It used to
take every 3MF whose name CONTAINED the print's name and keep the first that
downloaded — "Cube" got ``Cube_large.3mf``, and another print's thumbnail,
weights and usage figures landed on this print's archive.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import archive_download
from backend.app.services.archive_download import _comparable_names, build_filename_candidates, is_named_for_print


def _wanted(subtask: str | None, filename: str | None = None) -> frozenset[str]:
    return _comparable_names(build_filename_candidates(subtask, filename))


@pytest.mark.parametrize(
    ("listed", "subtask", "own"),
    [
        ("Cube.gcode.3mf", "Cube", True),
        ("Cube.3mf", "Cube", True),
        ("cube.GCODE.3MF", "Cube", True),  # the printer's own case
        ("Cable_Clip.gcode.3mf", "Cable Clip", True),  # its underscores for spaces
        ("Cable Clip.3mf", "Cable_Clip", True),
        ("Cube.gcode.3mf", "Cube.gcode.3mf", True),  # an A1 reports the card name as the subtask
        ("Cube_large.gcode.3mf", "Cube", False),
        ("Big Cube.3mf", "Cube", False),
        ("Cube_v2.gcode.3mf", "Cube", False),  # the only look-alike is still not the print's file
        ("Cube.gcode", "Cube", False),
        ("Cube.txt", "Cube", False),
    ],
)
def test_only_a_whole_name_is_the_prints_file(listed, subtask, own):
    assert is_named_for_print(listed, _wanted(subtask)) is own


def test_the_reported_file_name_counts_too():
    wanted = _wanted(None, "/data/Metadata/Lamp shade.gcode")
    assert is_named_for_print("Lamp_shade.gcode.3mf", wanted)
    assert not is_named_for_print("Lamp_shade_tall.gcode.3mf", wanted)


class _Client:
    """An FTP fake: nothing answers a direct probe; only listed paths download."""

    listed: set[str] = set()
    fetched: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        return True

    def download_to_file(self, remote_path, local_path):
        from backend.app.services.bambu_ftp import FileNotOnPrinterError

        _Client.fetched.append(remote_path)
        if remote_path not in _Client.listed:
            raise FileNotOnPrinterError(remote_path)
        Path(local_path).write_bytes(b"PK")
        return True

    def disconnect(self):
        pass


@pytest.fixture
def client(monkeypatch):
    import backend.app.services.bambu_ftp as bambu_ftp

    _Client.listed, _Client.fetched = set(), []
    monkeypatch.setattr(bambu_ftp, "BambuFTPClient", _Client)
    monkeypatch.setattr(archive_download, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))
    monkeypatch.setattr(archive_download, "_try_internal_storage", AsyncMock(return_value=None))
    return _Client


def _printer():
    return SimpleNamespace(id=7, ip_address="10.0.0.9", access_code="x", model="X1C", name="X1C")


@pytest.mark.asyncio
async def test_a_file_that_only_contains_the_name_is_never_downloaded(client, tmp_path, monkeypatch):
    client.listed = {"/cache/Cube_large.gcode.3mf"}

    async def listing(ip, code, directory, **kwargs):
        return [{"name": "Cube_large.gcode.3mf"}] if directory == "/cache" else []

    monkeypatch.setattr(archive_download, "list_files_async", listing)
    result = await archive_download.try_download_3mf(_printer(), "Cube", None, tmp_path)

    assert result is None
    assert not [path for path in client.fetched if "large" in path], client.fetched


@pytest.mark.asyncio
async def test_the_prints_own_file_in_the_printers_spelling_is_found(client, tmp_path, monkeypatch):
    # The card holds the name in another case, so no direct probe hits it; a
    # look-alike is listed first.
    client.listed = {"/cache/cable_clip_XL.gcode.3mf", "/model/CABLE_CLIP.gcode.3mf"}

    async def listing(ip, code, directory, **kwargs):
        return {
            "/cache": [{"name": "cable_clip_XL.gcode.3mf"}],
            "/model": [{"name": "CABLE_CLIP.gcode.3mf"}],
        }.get(directory, [])

    monkeypatch.setattr(archive_download, "list_files_async", listing)
    result = await archive_download.try_download_3mf(_printer(), "Cable Clip", None, tmp_path)

    assert result is not None
    assert result[1] == "CABLE_CLIP.gcode.3mf"
    assert not [path for path in client.fetched if "XL" in path], client.fetched
