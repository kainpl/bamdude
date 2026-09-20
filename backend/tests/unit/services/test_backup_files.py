"""Failures during backup/restore must never silently lose files or old state."""

import asyncio
import io
import os
import stat
import threading
import zipfile
from types import SimpleNamespace

import pytest

from backend.app.core.db_portable import _file_work
from backend.app.services import backup_files as files


def settings(root):
    return SimpleNamespace(
        base_dir=root,
        archive_dir=root / "custom-archive",
        library_dir=root / "library",
        projects_dir=root / "projects",
        products_dir=root / "products",
        plate_calibration_dir=root / "calibration",
    )


def transaction(tmp_path):
    staging, live = tmp_path / "staging", tmp_path / "live"
    (staging / "archive/empty").mkdir(parents=True)
    (staging / "archive/new.3mf").write_bytes(b"new-model")
    (staging / "icons").mkdir()
    (live / "custom-archive").mkdir(parents=True)
    (live / "custom-archive/old.3mf").write_bytes(b"old-model")
    (live / "icons").mkdir()
    (live / "icons/old.png").write_bytes(b"old-icon")
    return files.FileRestore(staging, settings(live), live), live


def assert_original(live):
    assert (live / "custom-archive/old.3mf").read_bytes() == b"old-model"
    assert not (live / "custom-archive/new.3mf").exists()
    assert (live / "icons/old.png").read_bytes() == b"old-icon"
    assert not list(live.rglob(".bamdude-restore-*"))


@pytest.mark.parametrize("commit", [False, True])
def test_restore_preserves_mount_root_and_empty_directories(tmp_path, commit):
    tx, live = transaction(tmp_path)
    inode = (live / "custom-archive").stat().st_ino
    tx.prepare()
    assert (live / "icons/old.png").exists()
    tx.apply()
    assert (live / "custom-archive/new.3mf").read_bytes() == b"new-model"
    assert not (live / "icons/old.png").exists()
    tx.committed = commit
    tx.finish()
    assert (live / "custom-archive").stat().st_ino == inode
    if commit:
        assert (live / "custom-archive/empty").is_dir()
        assert not list((live / "icons").iterdir())
        assert not list(live.rglob(".bamdude-restore-*"))
    else:
        assert_original(live)


@pytest.mark.parametrize("fail_move", [1, 2, 3, 4])
def test_rename_failure_rolls_back_every_completed_move(tmp_path, monkeypatch, fail_move):
    tx, live = transaction(tmp_path)
    tx.prepare()
    original, calls = os.replace, []

    def replace(source, target):
        calls.append(source)
        if len(calls) == fail_move:
            raise PermissionError("locked destination")
        return original(source, target)

    monkeypatch.setattr(files.os, "replace", replace)
    with pytest.raises(PermissionError):
        tx.apply()
    tx.finish()
    assert_original(live)


def test_full_disk_during_prepare_does_not_touch_live_files(tmp_path, monkeypatch):
    tx, live = transaction(tmp_path)

    def full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(files.os, "fsync", full)
    with pytest.raises(OSError, match="disk full"):
        tx.prepare()
    tx.finish()
    assert_original(live)


def test_rollback_failure_keeps_recovery_copies(tmp_path):
    tx, live = transaction(tmp_path)
    tx.prepare()
    tx.apply()
    # An external writer occupies the original path: don't overwrite its data
    # and don't delete the only old copy after the rollback reports failure.
    (live / "custom-archive/old.3mf").write_bytes(b"external-write")
    with pytest.raises(RuntimeError, match="recovery"):
        tx.finish()
    assert (live / "custom-archive/old.3mf").read_bytes() == b"external-write"
    assert (tx.swaps[0].work / "old/old.3mf").read_bytes() == b"old-model"
    assert (live / "icons/old.png").read_bytes() == b"old-icon"


def test_manifest_round_trip_includes_empty_and_custom_directories(tmp_path):
    live, staging, restored = tmp_path / "live", tmp_path / "stage", tmp_path / "restored"
    (live / "custom-archive/empty").mkdir(parents=True)
    (live / "custom-archive/model.3mf").write_bytes(b"model-data")
    staging.mkdir()
    restored.mkdir()
    files.stage_files(settings(live), live, staging)
    files.write_manifest(staging)
    output = tmp_path / "backup.zip"
    files.write_zip(staging, output)
    with output.open("rb") as stream:
        files.extract_zip(stream, restored)
    assert (restored / "archive/model.3mf").read_bytes() == b"model-data"
    assert (restored / "archive/empty").is_dir()
    assert (restored / "certs").is_dir()
    assert (restored / files.MANIFEST).read_bytes() == (staging / files.MANIFEST).read_bytes()


@pytest.mark.parametrize("damage", ["changed", "missing", "extra", "missing-directory"])
def test_manifest_rejects_incomplete_or_changed_files(tmp_path, damage):
    model = tmp_path / "model.3mf"
    model.write_bytes(b"model")
    (tmp_path / "empty").mkdir()
    files.write_manifest(tmp_path)
    if damage == "changed":
        model.write_bytes(b"other")
    elif damage == "missing":
        model.unlink()
    elif damage == "extra":
        (tmp_path / "extra").write_bytes(b"extra")
    else:
        (tmp_path / "empty").rmdir()
    with pytest.raises(ValueError, match="does not match"):
        files.verify_manifest(tmp_path)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../escape", "a\\escape", "a:stream", "NUL.txt", "a./b"])
def test_zip_rejects_unsafe_names_before_extraction(tmp_path, name):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("safe", b"first")
        archive.writestr(name, b"unsafe")
    # ZipInfo normalizes backslashes when constructing ZIPs on Windows.
    if "\\" in name:
        payload = io.BytesIO(payload.getvalue().replace(b"a/escape", b"a\\escape"))
    with pytest.raises(ValueError):
        files.extract_zip(payload, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("special", ["symlink", "duplicate"])
def test_zip_rejects_links_and_case_collisions(tmp_path, special):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("a", b"one")
        info = zipfile.ZipInfo("A" if special == "duplicate" else "link")
        if special == "symlink":
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"two")
    with pytest.raises(ValueError):
        files.extract_zip(payload, tmp_path)
    assert not list(tmp_path.iterdir())


def test_legacy_zip_without_manifest_still_checks_crc(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("model.3mf", b"unique-fixture")
    files.extract_zip(io.BytesIO(payload.getvalue()), tmp_path)
    assert (tmp_path / "model.3mf").read_bytes() == b"unique-fixture"
    (tmp_path / "model.3mf").unlink()
    damaged = payload.getvalue().replace(b"unique-fixture", b"broken-fixture")
    with pytest.raises(zipfile.BadZipFile):
        files.extract_zip(io.BytesIO(damaged), tmp_path)


def test_changing_source_is_a_failed_copy(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"original")
    original = files.os.fsync

    def change(fd):
        original(fd)
        source.write_bytes(b"changed-content")

    monkeypatch.setattr(files.os, "fsync", change)
    with pytest.raises(ValueError, match="Source changed"):
        files.copy_file(source, target)


def test_linked_source_and_destination_are_rejected(tmp_path):
    tx, live = transaction(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"must-survive")
    link = tx.targets[0][0] / "link"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="links"):
        tx.prepare()
    tx.finish()
    link.unlink()
    assert_original(live)
    (live / "icons/link").symlink_to(outside)
    tx = files.FileRestore(tmp_path / "staging", settings(live), live)
    with pytest.raises(ValueError, match="links"):
        tx.prepare()
    tx.finish()
    assert outside.read_bytes() == b"must-survive"
    assert_original(live)


def test_overlapping_destinations_fail_before_staging(tmp_path):
    tx, live = transaction(tmp_path)
    config = settings(live)
    config.archive_dir = live
    with pytest.raises(ValueError, match="Overlapping"):
        files.FileRestore(tmp_path / "staging", config, live)
    assert_original(live)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_junction_is_never_followed_or_deleted(tmp_path):
    import _winapi

    tx, live = transaction(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"keep")
    link = live / "icons/junction"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        with pytest.raises(ValueError, match="links"):
            tx.prepare()
        tx.finish()
        assert_original(live)
        assert (outside / "keep.bin").read_bytes() == b"keep"
    finally:
        link.rmdir()  # Remove only the junction, never its target tree.


@pytest.mark.asyncio
async def test_cancelled_apply_waits_for_worker_then_rolls_back(tmp_path, monkeypatch):
    tx, live = transaction(tmp_path)
    tx.prepare()
    started, release = threading.Event(), threading.Event()
    original = files._Swap.apply

    def apply(swap):
        original(swap)
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(files._Swap, "apply", apply)

    async def restore():
        try:
            await _file_work(tx.apply)
        finally:
            await _file_work(tx.finish)

    task = asyncio.create_task(restore())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_original(live)


@pytest.mark.asyncio
async def test_backup_restore_mutex_refuses_overlap_and_releases_after_error():
    with pytest.raises(RuntimeError, match="injected"):
        async with files.exclusive_operation():
            with pytest.raises(files.BackupBusyError):
                async with files.exclusive_operation():
                    pytest.fail("overlapping operation entered")
            raise RuntimeError("injected")
    async with files.exclusive_operation():
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_windows_open_file_without_delete_sharing_preserves_previous_state(tmp_path):
    import ctypes
    from ctypes import wintypes

    tx, live = transaction(tmp_path)
    tx.prepare()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(live / "icons/old.png"), 0x80000000, 1, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(PermissionError):
            tx.apply()
        tx.finish()
        assert_original(live)
    finally:
        kernel.CloseHandle(handle)
