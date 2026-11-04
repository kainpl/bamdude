"""Backup exactly the immutable originals named by the portable DB dump."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from backend.app.services.backup_files import _stat, copy_file, digest, inventory

ROOT_NAME = "obico-detections"


def _references(backup_db: Path) -> list[tuple[str, int, str]]:
    with closing(sqlite3.connect(backup_db)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='obico_events'").fetchone():
            return []  # old backup: an intentionally empty replacement root
        columns = {row[1] for row in db.execute('PRAGMA table_info("obico_events")')}
        if not {"id", "state", "size_bytes", "sha256"} <= columns:
            raise ValueError("Incomplete Obico evidence schema")
        rows = db.execute("SELECT id, size_bytes, sha256 FROM obico_events WHERE state='ready'").fetchall()
    result = []
    for event_id, size, sha in rows:
        try:
            parsed = UUID(event_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid Obico evidence identity") from exc
        if str(parsed) != event_id or type(size) is not int or not 0 < size <= 8 * 1024 * 1024:
            raise ValueError("Invalid Obico evidence reference")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("Invalid Obico evidence digest")
        result.append((parsed.hex + ".jpg", size, sha))
    return result


def _verify(path: Path, size: int, sha: str) -> None:
    if _stat(path).st_size != size or digest(path) != sha:
        raise ValueError("Obico evidence does not match the database snapshot")


def stage_history(data_dir: Path, staging: Path, backup_db: Path) -> None:
    source_root = data_dir / ROOT_NAME  # SEC-PATH-OK: ROOT_NAME is this module's constant.
    target_root = staging / ROOT_NAME
    target_root.mkdir(parents=True, exist_ok=True)
    references = _references(backup_db)
    if references:
        _stat(source_root)
    for name, size, sha in references:
        source = source_root / name  # SEC-PATH-OK: canonical UUID-only derived name.
        _stat(source)
        target = target_root / name  # SEC-PATH-OK: canonical UUID-only derived name.
        copy_file(source, target)
        _verify(target, size, sha)


def validate_history(staging: Path, backup_db: Path) -> None:
    root = staging / ROOT_NAME
    root.mkdir(exist_ok=True)  # legacy ZIP must clear the old root on restore
    references = _references(backup_db)
    actual = {name for name, (is_dir, _) in inventory(root).items() if name != "."}
    if actual != {name for name, _, _ in references}:
        raise ValueError("Obico evidence file set does not match the database snapshot")
    for name, size, sha in references:
        _verify(root / name, size, sha)  # SEC-PATH-OK: canonical UUID-only name.
