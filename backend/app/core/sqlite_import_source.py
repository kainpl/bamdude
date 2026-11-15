"""The supported SQLite-file → PostgreSQL boundary (not a SQLite upgrade gate).

Inspect a private DB+WAL copy: even SQLite's mode=ro may create SHM beside its
input. The caller owns the blocking operation through db_portable._file_work.
"""

import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

MIN_IMPORT_VERSION = 41  # v0.4.2; earlier AS-IS chains need additional dialect work.
SOURCE_LEVEL_MESSAGE = (
    "This SQLite source is older than the supported PostgreSQL import level or has invalid migration history. "
    "Upgrade a COPY of the SQLite database to a supported BamDude version and create a new backup."
)
SOURCE_READ_MESSAGE = (
    "Unable to read or copy the SQLite source. Check free space and access permissions "
    "for the database and temporary directory, then retry."
)


class SqliteSourceLevelError(ValueError):
    code = "sqlite_source_below_supported_level"

    def __init__(self):
        super().__init__(SOURCE_LEVEL_MESSAGE)


class SqliteSourceReadError(ValueError):
    code = "sqlite_source_read_failed"

    def __init__(self):
        super().__init__(SOURCE_READ_MESSAGE)


def _stamp(path: Path):
    try:
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ino
    except FileNotFoundError:
        return None


@contextmanager
def copied_sqlite_source(path: Path):
    """Never open the original with SQLite, or accept a changing DB/WAL pair."""
    paths = (path, path.with_name(path.name + "-wal"))
    before = tuple(_stamp(p) for p in paths)
    if before[0] is None:
        raise ValueError("SQLite source is missing")
    with tempfile.TemporaryDirectory(prefix="bamdude-import-check-") as directory:
        target = Path(directory) / "source.db"
        shutil.copyfile(path, target)
        if before[1] is not None:
            shutil.copyfile(paths[1], target.with_name(target.name + "-wal"))
        if before != tuple(_stamp(p) for p in paths):
            raise ValueError("SQLite source changed during preflight; stop its writer and retry")
        with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as source:
            yield source


def validate_postgres_source(path: Path) -> None:
    """Reject unsupported/unproven levels before target DDL or source consumption."""
    from backend.app.migrations import _discover_migrations

    try:
        with copied_sqlite_source(path) as source:
            rows = source.execute("SELECT version FROM _migrations").fetchall()
            versions = [row[0] for row in rows]
            if not versions or any(type(v) is not int or v < 0 for v in versions):
                raise SqliteSourceLevelError()
            if max(versions) < MIN_IMPORT_VERSION or len(versions) != len(set(versions)):
                raise SqliteSourceLevelError()
            if set(versions) - {m["version"] for m in _discover_migrations()}:
                raise SqliteSourceLevelError()
    except sqlite3.Error as exc:
        raise SqliteSourceLevelError() from exc
    except OSError as exc:
        raise SqliteSourceReadError() from exc


def is_legacy_bamdude_source(path: Path) -> bool:
    try:
        with copied_sqlite_source(path) as source:
            return (
                source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_chats'").fetchone()
                is not None
            )
    except OSError as exc:
        # Lack of temp space/access proves nothing about the file's identity.
        raise SqliteSourceReadError() from exc
    except (sqlite3.Error, ValueError):
        return False
