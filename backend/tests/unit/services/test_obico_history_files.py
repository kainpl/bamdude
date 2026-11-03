"""Exact backup references and replacing a legacy evidence root on restore."""

import hashlib
import sqlite3
from uuid import uuid4

import pytest

from backend.app.core.config import Settings
from backend.app.services.backup_files import FileRestore
from backend.app.services.obico_history_files import stage_history, validate_history


def backup_db(path, payload=b"jpeg", *, legacy=False, event_id=None):
    event_id = event_id or str(uuid4())
    with sqlite3.connect(path) as db:
        if legacy:
            db.execute("CREATE TABLE old_backup(id INTEGER)")
        else:
            db.execute("CREATE TABLE obico_events(id TEXT, state TEXT, size_bytes INTEGER, sha256 TEXT)")
            db.execute(
                "INSERT INTO obico_events VALUES (?, 'ready', ?, ?)",
                (event_id, len(payload), hashlib.sha256(payload).hexdigest()),
            )
    return event_id.replace("-", "") + ".jpg"


@pytest.mark.parametrize("fault", ["missing", "corrupt", "extra", "path", "directory"])
def test_invalid_backup_refuses_before_restore(tmp_path, fault):
    staging = tmp_path / "staging"
    root = staging / "obico-detections"
    root.mkdir(parents=True)
    db = tmp_path / "backup.db"
    name = backup_db(db, event_id="../../escape" if fault == "path" else None)
    if fault not in ("missing", "path"):
        (root / name).write_bytes(b"wrong" if fault == "corrupt" else b"jpeg")
    if fault == "extra":
        (root / "unreferenced.jpg").write_bytes(b"jpeg")
    if fault == "directory":
        (root / "nested").mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_history(staging, db)


@pytest.mark.parametrize("committed", [False, True])
def test_legacy_empty_root_replaces_old_history_and_rollback_restores_it(tmp_path, committed):
    live, staging = tmp_path / "live", tmp_path / "staging"
    old_root = live / "obico-detections"
    old_root.mkdir(parents=True)
    (old_root / "old.jpg").write_bytes(b"old")
    staging.mkdir()
    db = tmp_path / "legacy.db"
    backup_db(db, legacy=True)
    validate_history(staging, db)
    settings = Settings(_env_file=None, data_dir=live)
    files = FileRestore(staging, settings, live)
    files.prepare()
    files.apply()
    assert not (old_root / "old.jpg").exists()  # rollback staging lives inside the mount until finish
    files.committed = committed
    files.finish()
    assert list(old_root.iterdir()) == ([] if committed else [old_root / "old.jpg"])


def test_staging_excludes_newer_unreferenced_files_and_detects_corrupt_original(tmp_path):
    data, staging = tmp_path / "data", tmp_path / "staging"
    root = data / "obico-detections"
    root.mkdir(parents=True)
    name = backup_db(tmp_path / "backup.db")
    (root / name).write_bytes(b"jpeg")
    (root / (uuid4().hex + ".jpg")).write_bytes(b"newer frame")
    (root / (uuid4().hex + ".part")).write_bytes(b"in-flight")
    stage_history(data, staging, tmp_path / "backup.db")
    assert [p.name for p in (staging / "obico-detections").iterdir()] == [name]
    validate_history(staging, tmp_path / "backup.db")
    (root / name).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        stage_history(data, tmp_path / "bad-staging", tmp_path / "backup.db")
