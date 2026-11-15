"""The supported SQLite-file → PostgreSQL boundary (not a SQLite upgrade gate).

Inspect a private DB+WAL copy: even SQLite's mode=ro may create SHM beside its
input. The caller owns the blocking operation through db_portable._file_work.
"""

import hashlib
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


def _fingerprint(path: Path):
    """No SQLite open against the original, including its WAL."""
    result = []
    for item in (path, path.with_name(path.name + "-wal")):
        stamp = _stamp(item)
        if stamp is None:
            result.append(None)
            continue
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if _stamp(item) != stamp:
            raise ValueError("SQLite source changed during preflight; stop its writer and retry")
        result.append((stamp, digest.hexdigest()))
    return tuple(result)


class PreparedSqliteSource:
    """One owned immutable DB+WAL copy for identity, validation and import.

    Creation/cleanup and checks run through the caller's owned file worker.
    Each reader opens only the private copy, so readers need not share a
    thread-affine sqlite3 connection. The original is never opened by SQLite.
    """

    def __init__(self, original: Path):
        self.original = Path(original)
        self.metadata = None
        self._directory = tempfile.TemporaryDirectory(prefix="bamdude-import-check-")
        self.path = Path(self._directory.name) / "source.db"
        self._copied = False

    def copy(self):
        try:
            self.fingerprint = _fingerprint(self.original)
            if self.fingerprint[0] is None:
                raise ValueError("SQLite source is missing")
            shutil.copyfile(self.original, self.path)
            if self.fingerprint[1] is not None:
                shutil.copyfile(
                    self.original.with_name(self.original.name + "-wal"),
                    self.path.with_name(self.path.name + "-wal"),
                )
            self.assert_unchanged()
            self._copied = True
        except BaseException:
            self.close()
            raise

    def assert_unchanged(self):
        if _fingerprint(self.original) != self.fingerprint:
            raise ValueError("SQLite source changed during preflight; stop its writer and retry")

    def close(self):
        self._directory.cleanup()

    def validate(self, models_metadata):
        from backend.app.core.db_portable import _reflect_sqlite_schema, validate_sqlite_backup
        from backend.app.core.sqlite_import_preflight import validate_text_values

        validate_postgres_source(self)
        validate_sqlite_backup(self.path)
        self.metadata = _reflect_sqlite_schema(self.path, models_metadata)
        validate_text_values(self.path, self.metadata)


def prepare_postgres_source(path: Path | PreparedSqliteSource, models_metadata) -> PreparedSqliteSource:
    source = path if isinstance(path, PreparedSqliteSource) else None
    try:
        if source is None:
            source = PreparedSqliteSource(path)
        if not source._copied:
            source.copy()
        source.validate(models_metadata)
        return source
    except BaseException as exc:
        if source is not None:
            source.close()
        if isinstance(exc, OSError):
            raise SqliteSourceReadError() from exc
        raise


@contextmanager
def copied_sqlite_source(path: Path | PreparedSqliteSource):
    """Never open the original with SQLite, or accept a changing DB/WAL pair."""
    prepared = path if isinstance(path, PreparedSqliteSource) else PreparedSqliteSource(path)
    try:
        if not prepared._copied:
            prepared.copy()
        with closing(sqlite3.connect(f"{prepared.path.as_uri()}?mode=ro", uri=True)) as source:
            yield source
    finally:
        if prepared is not path:
            prepared.close()


def validate_postgres_source(path: Path | PreparedSqliteSource) -> None:
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


def is_legacy_bamdude_source(path: Path | PreparedSqliteSource) -> bool:
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
