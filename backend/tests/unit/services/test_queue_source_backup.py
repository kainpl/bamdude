"""The portable backup carries exactly the queue blobs its database names."""

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.services import backup_files


def _record(db_path: Path, payload: bytes, *, state: str = "ready") -> tuple[str, str]:
    sha256 = hashlib.sha256(payload).hexdigest()
    relative = f"queue-spool/objects/{sha256[:2]}/{sha256}.3mf"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE queue_sources (
                sha256 TEXT, size_bytes INTEGER, relative_path TEXT, format TEXT, state TEXT
            )"""
        )
        db.execute(
            "INSERT INTO queue_sources VALUES (?, ?, ?, ?, ?)",
            (sha256, len(payload), relative, "3mf", state),
        )
    return sha256, relative


def test_stage_queue_spool_uses_the_database_snapshot_and_excludes_parts(tmp_path):
    data, staging, backup_db = tmp_path / "data", tmp_path / "staging", tmp_path / "backup.db"
    payload = b"job bytes"
    _sha256, relative = _record(backup_db, payload)
    source = data / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    (data / "queue-spool/staging").mkdir(parents=True)
    (data / "queue-spool/staging/live.part").write_bytes(b"not published")
    (data / "queue-spool/objects/ff").mkdir(parents=True)
    (data / "queue-spool/objects/ff/unreferenced.3mf").write_bytes(b"not in database")

    backup_files.stage_queue_spool(data, staging, backup_db)

    assert (staging / relative).read_bytes() == payload
    assert not (staging / "queue-spool/staging/live.part").exists()
    assert not (staging / "queue-spool/objects/ff/unreferenced.3mf").exists()
    backup_files.validate_staged_queue_spool(staging, backup_db)


def test_stage_queue_spool_fails_when_a_referenced_object_is_missing_or_wrong(tmp_path):
    data, staging, backup_db = tmp_path / "data", tmp_path / "staging", tmp_path / "backup.db"
    _sha256, relative = _record(backup_db, b"recorded bytes")
    source = data / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b"different bytes")

    with pytest.raises(ValueError, match="Queue source"):
        backup_files.stage_queue_spool(data, staging, backup_db)


def test_restore_preflight_rejects_a_database_reference_missing_from_the_zip(tmp_path):
    staging, backup_db = tmp_path / "staging", tmp_path / "backup.db"
    _record(backup_db, b"required object")
    (staging / "queue-spool/objects").mkdir(parents=True)

    with pytest.raises(ValueError, match="queue spool"):
        backup_files.validate_staged_queue_spool(staging, backup_db)


def test_file_restore_swaps_the_queue_spool_as_one_directory(tmp_path):
    live, staging = tmp_path / "live", tmp_path / "staging"
    (live / "queue-spool/objects/old").mkdir(parents=True)
    (live / "queue-spool/objects/old/job.3mf").write_bytes(b"old")
    (staging / "queue-spool/objects/new").mkdir(parents=True)
    (staging / "queue-spool/objects/new/job.3mf").write_bytes(b"new")
    settings = SimpleNamespace(
        base_dir=live,
        archive_dir=live / "archive",
        plate_calibration_dir=live / "plate_calibration",
    )
    restore = backup_files.FileRestore(staging, settings, live)

    restore.prepare()
    restore.apply()
    restore.committed = True
    restore.finish()

    assert (live / "queue-spool/objects/new/job.3mf").read_bytes() == b"new"
    assert not (live / "queue-spool/objects/old/job.3mf").exists()
