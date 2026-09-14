"""A rejected/failed restore must keep the previous database's key and identity."""

import asyncio
import io
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile

from backend.app.api.routes import settings as route


def upload(tmp_path, *, damaged=False):
    source = tmp_path / "source.db"
    if damaged:
        source.write_bytes(b"corrupt")
    else:
        with closing(sqlite3.connect(source)) as db:
            db.executescript("CREATE TABLE printers(id INTEGER PRIMARY KEY); CREATE TABLE settings(key TEXT);")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.write(source, "bamdude.db")
        archive.writestr(".mfa_encryption_key", b"backup-key")
        archive.writestr(".install_id", b"backup-identity")
        archive.write(source, "zigbee/zigbee.db")
        archive.writestr("archive/restored.3mf", b"restored-model")
        archive.writestr("icons/", b"")
    payload.seek(0)
    return UploadFile(file=payload, filename="backup.zip")


@pytest.mark.asyncio
async def test_corrupt_backup_is_rejected_before_service_or_database_changes(tmp_path, monkeypatch):
    stop = AsyncMock()
    monkeypatch.setattr(
        "backend.app.services.virtual_printer.virtual_printer_manager", SimpleNamespace(is_enabled=True, configure=stop)
    )
    close = AsyncMock()
    monkeypatch.setattr("backend.app.core.database.close_all_connections", close)
    with pytest.raises(HTTPException) as caught:
        await route.restore_backup(file=upload(tmp_path, damaged=True), db=AsyncMock(), _=None)
    assert caught.value.status_code == 400
    stop.assert_not_awaited()
    close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, b"previous-key"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_database_swap_restores_previous_key_and_keeps_identity(tmp_path, monkeypatch, previous, cancel):
    live = tmp_path / "live"
    (live / "zigbee").mkdir(parents=True)
    (live / "custom-archive").mkdir()
    (live / "custom-archive/old.3mf").write_bytes(b"old-model")
    (live / "icons").mkdir()
    (live / "icons/old.png").write_bytes(b"old-icon")
    key = live / ".mfa_encryption_key"
    if previous is not None:
        key.write_bytes(previous)
    (live / ".install_id").write_bytes(b"previous-identity")
    (live / "zigbee/zigbee.db").write_bytes(b"previous-network")
    monkeypatch.setenv("DATA_DIR", str(live))
    monkeypatch.setattr(route.app_settings, "base_dir", live)
    monkeypatch.setattr(route.app_settings, "archive_dir", live / "custom-archive")
    monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: False)
    monkeypatch.setattr(
        "backend.app.services.virtual_printer.virtual_printer_manager", SimpleNamespace(is_enabled=False)
    )
    monkeypatch.setattr("backend.app.services.print_scheduler.scheduler", SimpleNamespace(stop=Mock()))
    monkeypatch.setattr(
        "backend.app.services.smart_plug_manager.smart_plug_manager", SimpleNamespace(stop_scheduler=Mock())
    )
    monkeypatch.setattr(
        "backend.app.services.notification_service.notification_service", SimpleNamespace(stop_digest_scheduler=Mock())
    )
    monkeypatch.setattr(
        "backend.app.services.background_dispatch.background_dispatch", SimpleNamespace(stop=AsyncMock())
    )
    monkeypatch.setattr("backend.app.core.database.close_all_connections", AsyncMock())

    async def failed_import(*args):
        assert key.read_bytes() == b"backup-key"
        assert (live / "custom-archive/restored.3mf").read_bytes() == b"restored-model"
        assert not (live / "icons/old.png").exists()
        raise asyncio.CancelledError if cancel else RuntimeError("injected database failure")

    monkeypatch.setattr("backend.app.core.db_portable.import_sqlite_to_postgres", failed_import)
    session = AsyncMock()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await route.restore_backup(file=upload(tmp_path), db=session, _=None)
    else:
        response = await route.restore_backup(file=upload(tmp_path), db=session, _=None)
        assert response.status_code == 500
    session.close.assert_awaited_once()
    assert (key.read_bytes() if key.exists() else None) == previous
    assert (live / ".install_id").read_bytes() == b"previous-identity"
    assert (live / "zigbee/zigbee.db").read_bytes() == b"previous-network"
    assert (live / "custom-archive/old.3mf").read_bytes() == b"old-model"
    assert not (live / "custom-archive/restored.3mf").exists()
    assert (live / "icons/old.png").read_bytes() == b"old-icon"
    assert not list(live.rglob(".bamdude-restore-*"))


@pytest.mark.asyncio
async def test_zip_write_failure_preserves_previous_backup(tmp_path, monkeypatch):
    live, output = tmp_path / "live", tmp_path / "backups"
    live.mkdir()
    output.mkdir()
    monkeypatch.setenv("DATA_DIR", str(live))
    monkeypatch.setattr(route.app_settings, "base_dir", live)
    monkeypatch.setattr(route.app_settings, "archive_dir", live / "custom-archive")
    monkeypatch.setattr(route.app_settings, "plate_calibration_dir", live / "plate_calibration")
    monkeypatch.setattr(route, "datetime", SimpleNamespace(now=lambda: datetime(2026, 9, 11)))

    async def dump(engine, metadata, destination):
        # The production exporter always writes SQLite. The queue-spool stage
        # now reads that portable snapshot, so this focused ZIP-write test uses
        # the smallest valid snapshot rather than opaque placeholder bytes.
        with closing(sqlite3.connect(destination)) as db:
            db.execute(
                """CREATE TABLE queue_sources (
                    sha256 TEXT, size_bytes INTEGER, relative_path TEXT, format TEXT, state TEXT
                )"""
            )

    monkeypatch.setattr("backend.app.core.db_portable.dump_to_sqlite", dump)
    path, _ = await route.create_backup_zip(output)
    previous = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected ZIP write failure")

    monkeypatch.setattr(zipfile.ZipFile, "write", fail)
    with pytest.raises(OSError, match="injected"):
        await route.create_backup_zip(output)
    assert path.read_bytes() == previous
    assert list(output.iterdir()) == [path]


@pytest.mark.asyncio
async def test_copy_failure_aborts_before_database_swap(tmp_path, monkeypatch):
    from backend.app.services.backup_files import FileRestore

    def fail_copy(self):
        raise OSError("injected full disk")

    monkeypatch.setattr(FileRestore, "prepare", fail_copy)
    replace = AsyncMock()
    monkeypatch.setattr("backend.app.core.db_portable.import_sqlite_to_postgres", replace)
    session = AsyncMock()
    response = await route.restore_backup(file=upload(tmp_path), db=session, _=None)
    assert response.status_code == 500
    replace.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_backup_and_restore_refuse_running_operation(tmp_path):
    from backend.app.services.backup_files import exclusive_operation

    async with exclusive_operation():
        for operation in (
            route.create_backup(db=AsyncMock(), _=None),
            route.restore_backup(file=upload(tmp_path), db=AsyncMock(), _=None),
        ):
            with pytest.raises(HTTPException) as caught:
                await operation
            assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_unreadable_source_fails_backup_instead_of_publishing_partial_zip(tmp_path, monkeypatch):
    from backend.app.services import backup_files

    live, output = tmp_path / "live", tmp_path / "backups"
    live.mkdir()
    output.mkdir()
    monkeypatch.setenv("DATA_DIR", str(live))
    monkeypatch.setattr(route.app_settings, "base_dir", live)
    monkeypatch.setattr(route.app_settings, "archive_dir", live / "archive")
    (live / "archive").mkdir()
    (live / "archive/model.3mf").write_bytes(b"model")

    async def dump(engine, metadata, destination):
        destination.write_bytes(b"synthetic db")

    def denied(*args):
        raise PermissionError("injected unreadable file")

    monkeypatch.setattr("backend.app.core.db_portable.dump_to_sqlite", dump)
    monkeypatch.setattr(backup_files, "copy_file", denied)
    with pytest.raises(PermissionError):
        await route.create_backup_zip(output)
    assert not list(output.iterdir())


@pytest.mark.asyncio
async def test_cancel_after_sqlite_commit_keeps_matching_new_files(tmp_path, monkeypatch):
    import threading

    from backend.app.core import db_portable

    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setenv("DATA_DIR", str(live))
    monkeypatch.setattr(route.app_settings, "base_dir", live)
    monkeypatch.setattr(route.app_settings, "archive_dir", live / "archive")
    monkeypatch.setattr(route.app_settings, "database_url", f"sqlite+aiosqlite:///{live / 'bamdude.db'}")
    monkeypatch.setattr("backend.app.core.database.close_all_connections", AsyncMock())
    original = db_portable._snapshot_sqlite
    copied, release = threading.Event(), threading.Event()

    def copy_then_wait(source, target):
        original(source, target)
        copied.set()
        assert release.wait(5)

    monkeypatch.setattr(db_portable, "_snapshot_sqlite", copy_then_wait)
    task = asyncio.create_task(route.restore_backup(file=upload(tmp_path), db=AsyncMock(), _=None))
    assert await asyncio.to_thread(copied.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with closing(sqlite3.connect(live / "bamdude.db")) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='printers'").fetchone() == ("printers",)
    assert (live / ".mfa_encryption_key").read_bytes() == b"backup-key"
    assert (live / "archive/restored.3mf").read_bytes() == b"restored-model"
    assert not list(live.rglob(".bamdude-restore-*"))
