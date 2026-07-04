"""Tests for _cleanup_forced_timelapse (#1397).

When BamDude forced timelapse on for the finish-photo path, this helper
runs after the extractor (success OR failure — we never leave debris).
It deletes:
  - the locally-attached file (clears archive.timelapse_path)
  - the printer-side file via FTP DELE, walking the four scanner dirs

These tests pin the four branches:

  1. archive doesn't exist → no-op
  2. archive exists but bamdude_forced_timelapse=False → no-op (user wanted
     the timelapse)
  3. archive exists, forced=True, local file present → delete local + DB
     update + FTP DELE on the first directory that succeeds
  4. archive exists, forced=True, but FTP DELE fails on every dir → local
     side still cleaned up; warn log emitted (best-effort)
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app import main as main_module
from backend.app.main import _cleanup_forced_timelapse


def _fake_session_factory(rows: dict):
    """Return an async_session() replacement that yields the given rows.

    `rows` is a mapping of model -> object that the test wants returned
    from `db.execute(select(...)).scalar_one_or_none()`. The select
    target is detected by walking the column descriptions — for these
    tests we just look at the model class name.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session():
        async def execute(stmt):
            # The select(...) statement carries the target entity in
            # `stmt.column_descriptions[0]["entity"]`. Match by class name.
            target_name = stmt.column_descriptions[0]["entity"].__name__
            row = rows.get(target_name)
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        commits: list[None] = []

        async def commit():
            commits.append(None)

        yield SimpleNamespace(execute=execute, commit=commit, _commits=commits)

    return fake_session


@pytest.fixture(autouse=True)
def patch_app_settings(monkeypatch, tmp_path):
    """Point base_dir at a tmp_path so the helper can resolve relative
    timelapse paths against a real fs we control."""
    monkeypatch.setattr(main_module.app_settings, "base_dir", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_no_archive_is_noop(monkeypatch):
    """Archive deleted between print start and cleanup? Don't crash."""
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory({"PrintArchive": None, "Printer": None}))
    delete_mock = AsyncMock()
    with patch("backend.app.services.bambu_ftp.delete_file_async", new=delete_mock):
        await _cleanup_forced_timelapse(archive_id=99, printer_id=10)
    delete_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_forced_is_noop(monkeypatch, tmp_path):
    """User wanted a timelapse → don't delete anything."""
    archive = SimpleNamespace(
        bamdude_forced_timelapse=False,
        timelapse_path="archive/1/timelapse.mp4",
    )
    monkeypatch.setattr(
        main_module,
        "async_session",
        _fake_session_factory({"PrintArchive": archive, "Printer": None}),
    )

    # Lay down a real file so we'd detect a stray delete.
    video_path = tmp_path / archive.timelapse_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"x" * 100)

    delete_mock = AsyncMock(return_value=True)
    with patch("backend.app.services.bambu_ftp.delete_file_async", new=delete_mock):
        await _cleanup_forced_timelapse(archive_id=99, printer_id=10)

    delete_mock.assert_not_awaited()
    assert video_path.exists()
    # archive.timelapse_path is untouched — we still have the user's video
    # tracked correctly.
    assert archive.timelapse_path == "archive/1/timelapse.mp4"


@pytest.mark.asyncio
async def test_forced_deletes_local_and_remote(monkeypatch, tmp_path):
    """Happy path: forced=True → local file unlinked, DB row cleared, FTP
    DELE called against /timelapse/<filename> (the first dir to succeed)."""
    archive = SimpleNamespace(
        bamdude_forced_timelapse=True,
        timelapse_path="archive/1/myprint.mp4",
    )
    printer = SimpleNamespace(ip_address="10.0.0.5", access_code="12345678", model="O1C")
    monkeypatch.setattr(
        main_module,
        "async_session",
        _fake_session_factory({"PrintArchive": archive, "Printer": printer}),
    )

    video_path = tmp_path / archive.timelapse_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"x" * 100)

    # FTP DELE succeeds on the first directory we try.
    delete_mock = AsyncMock(return_value=True)
    with patch("backend.app.services.bambu_ftp.delete_file_async", new=delete_mock):
        await _cleanup_forced_timelapse(archive_id=99, printer_id=10)

    # Local side: file gone, DB cleared.
    assert not video_path.exists()
    assert archive.timelapse_path is None
    # Remote side: DELE'd against /timelapse/myprint.mp4 — that's the
    # first dir the cleanup tries.
    delete_mock.assert_awaited()
    call = delete_mock.await_args
    assert call.args[0] == "10.0.0.5"
    assert call.args[1] == "12345678"
    assert call.args[2] == "/timelapse/myprint.mp4"


@pytest.mark.asyncio
async def test_forced_walks_alternate_dirs_when_first_fails(monkeypatch, tmp_path):
    """If /timelapse/ DELE returns False (file not there), try the other
    scanner dirs in order."""
    archive = SimpleNamespace(
        bamdude_forced_timelapse=True,
        timelapse_path="archive/1/myprint.mp4",
    )
    printer = SimpleNamespace(ip_address="10.0.0.5", access_code="12345678", model="O1C")
    monkeypatch.setattr(
        main_module,
        "async_session",
        _fake_session_factory({"PrintArchive": archive, "Printer": printer}),
    )

    video_path = tmp_path / archive.timelapse_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"x" * 100)

    # First two attempts fail (False), third succeeds (True). Cleanup
    # should stop after the third.
    delete_mock = AsyncMock(side_effect=[False, False, True])
    with patch("backend.app.services.bambu_ftp.delete_file_async", new=delete_mock):
        await _cleanup_forced_timelapse(archive_id=99, printer_id=10)

    assert delete_mock.await_count == 3
    paths_tried = [call.args[2] for call in delete_mock.await_args_list]
    assert paths_tried == [
        "/timelapse/myprint.mp4",
        "/timelapse/video/myprint.mp4",
        "/record/myprint.mp4",
    ]


@pytest.mark.asyncio
async def test_forced_local_cleanup_runs_even_if_ftp_unreachable(monkeypatch, tmp_path):
    """FTP completely failing must not block local cleanup — the user's
    archive UI should reflect that the timelapse is gone immediately,
    even if the printer-side file lingers."""
    archive = SimpleNamespace(
        bamdude_forced_timelapse=True,
        timelapse_path="archive/1/myprint.mp4",
    )
    printer = SimpleNamespace(ip_address="10.0.0.5", access_code="12345678", model="O1C")
    monkeypatch.setattr(
        main_module,
        "async_session",
        _fake_session_factory({"PrintArchive": archive, "Printer": printer}),
    )

    video_path = tmp_path / archive.timelapse_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"x" * 100)

    # Every FTP attempt throws.
    delete_mock = AsyncMock(side_effect=OSError("connection refused"))
    with patch("backend.app.services.bambu_ftp.delete_file_async", new=delete_mock):
        await _cleanup_forced_timelapse(archive_id=99, printer_id=10)

    # Local side cleaned up even though all FTP attempts threw.
    assert not video_path.exists()
    assert archive.timelapse_path is None
    # All four dirs were attempted before giving up.
    assert delete_mock.await_count == 4
