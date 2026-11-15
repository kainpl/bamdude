"""The PG import floor must refuse before any destructive operation."""

import gzip
import hashlib
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.core.sqlite_import_source import (
    SqliteSourceLevelError,
    SqliteSourceReadError,
    is_legacy_bamdude_source,
    validate_postgres_source,
)


def files(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}


@pytest.mark.parametrize("versions", [None, [], [21], [40], [None], ["bad"], [41, 41], [999], [40, 999]])
def test_refused_source_does_not_create_sidecars(tmp_path, versions):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as source:
        source.execute("CREATE TABLE telegram_chats (id INTEGER)")
        source.execute("PRAGMA journal_mode=WAL")
        if versions is not None:
            source.execute("CREATE TABLE _migrations (version)")
            source.executemany("INSERT INTO _migrations VALUES (?)", [(v,) for v in versions])
    # No connection remains. Opening the original mode=ro would recreate SHM.
    before = files(tmp_path)
    with pytest.raises(SqliteSourceLevelError, match="Upgrade a COPY"):
        validate_postgres_source(path)
    assert is_legacy_bamdude_source(path)
    assert files(tmp_path) == before


def test_committed_wal_is_read_without_opening_source(tmp_path):
    path = tmp_path / "source.db"
    source = sqlite3.connect(path)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE _migrations (version INTEGER)")
        source.execute("INSERT INTO _migrations VALUES (41)")
        source.commit()
        before = files(tmp_path)
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        validate_postgres_source(path)
        assert files(tmp_path) == before
    finally:
        source.close()


@pytest.mark.asyncio
async def test_direct_import_refuses_before_first_target_connection(tmp_path):
    from backend.app.core.db_portable import import_sqlite_to_postgres

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as source:
        source.execute("CREATE TABLE _migrations (version INTEGER)")
        source.execute("INSERT INTO _migrations VALUES (21)")
    engine = AsyncMock()
    before = files(tmp_path)
    with pytest.raises(SqliteSourceLevelError):
        await import_sqlite_to_postgres(engine, None, path)
    assert engine.mock_calls == []
    assert files(tmp_path) == before


@pytest.mark.asyncio
async def test_startup_floor_precedes_create_all(monkeypatch, tmp_path):
    from backend.app import migrations
    from backend.app.core import db_portable
    from backend.app.core.config import settings

    async def refuse(_engine):
        raise db_portable.SqliteImportError("unsupported source")

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused/unused")
    monkeypatch.setattr(db_portable, "preflight_sqlite_to_pg", refuse)
    engine = AsyncMock()
    with pytest.raises(db_portable.SqliteImportError):
        await migrations.run_all_migrations(engine, None)
    assert engine.mock_calls == []


def test_copy_cleanup_after_error(tmp_path, monkeypatch):
    import backend.app.core.sqlite_import_source as boundary

    path = tmp_path / "source.db"
    path.write_bytes(b"not a database")
    before = files(tmp_path)
    with pytest.raises(SqliteSourceLevelError):
        boundary.validate_postgres_source(path)
    assert files(tmp_path) == before


@pytest.mark.parametrize("name,supported", [("v040", False), ("v042", True)])
def test_actual_pinned_sources_are_untouched(tmp_path, name, supported):
    source = Path(__file__).parents[1] / "integration/fixtures/g02_pg_schema" / f"{name}.db.gz"
    path = tmp_path / "bamdude.db"
    path.write_bytes(gzip.decompress(source.read_bytes()))
    before = files(tmp_path)
    if supported:
        validate_postgres_source(path)
    else:
        with pytest.raises(SqliteSourceLevelError):
            validate_postgres_source(path)
    assert files(tmp_path) == before


@pytest.mark.asyncio
async def test_copy_failure_is_actionable_and_does_not_misidentify_legacy_file(tmp_path, monkeypatch, caplog):
    from backend.app.core import db_portable, sqlite_import_source
    from backend.app.core.config import settings

    path = tmp_path / "bambuddy.db"
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE telegram_chats(id INTEGER)")
        source.execute("CREATE TABLE _migrations(version INTEGER)")
        source.execute("INSERT INTO _migrations VALUES(41)")
        source.commit()
    before = files(tmp_path)

    def full(*_args):
        raise OSError(28, "private-temp-path: no space left")

    monkeypatch.setattr(sqlite_import_source.shutil, "copyfile", full)
    with pytest.raises(SqliteSourceReadError, match="free space") as refused:
        validate_postgres_source(path)
    assert "private-temp-path" not in str(refused.value)
    with pytest.raises(SqliteSourceReadError):
        await db_portable._local_sqlite_candidate(tmp_path)
    assert "foreign" not in caplog.text.lower() and "Found a Bambuddy database" not in caplog.text

    @asynccontextmanager
    async def connect():
        yield SimpleNamespace(run_sync=AsyncMock(return_value=False))

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused/unused")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with pytest.raises(db_portable.SqliteImportError, match="temporary directory"):
        await db_portable.preflight_sqlite_to_pg(SimpleNamespace(connect=connect))
    assert files(tmp_path) == before
