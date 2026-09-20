"""Portable files must be complete, publish atomically, and keep bootstrap metadata intact."""

import asyncio
import sqlite3
import threading
from contextlib import closing

import pytest
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import db_portable


@pytest.mark.asyncio
async def test_export_omits_migration_shims_and_preserves_history_and_json(tmp_path):
    metadata = MetaData()
    table = Table(
        "sample",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("payload", JSON),
        Column("removed", String, info={"migration_shim": True}),
    )
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript("""
            CREATE TABLE sample (id INTEGER PRIMARY KEY, payload JSON);
            INSERT INTO sample VALUES (1, 'false'), (2, '"text"'), (3, '[1, {"uk": "так"}]'), (4, NULL), (5, 'null');
            CREATE TABLE _migrations (id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE,
                                      name VARCHAR(100) NOT NULL, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO _migrations VALUES (17, 1, 'baseline', '2020-01-02 03:04:05');
        """)
    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    output = tmp_path / "backup.db"
    try:
        await db_portable._export_pg_to_sqlite(engine, metadata, output)
    finally:
        await engine.dispose()
    assert "removed" in table.c, "export must not mutate global bootstrap metadata"
    with closing(sqlite3.connect(output)) as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(sample)")] == ["id", "payload"]
        assert conn.execute("SELECT * FROM _migrations").fetchall() == [(17, 1, "baseline", "2020-01-02 03:04:05")]
        import json

        assert [json.loads(r[0]) for r in conn.execute("SELECT payload FROM sample WHERE id <= 3 ORDER BY id")] == [
            False,
            "text",
            [1, {"uk": "так"}],
        ]
        assert conn.execute("SELECT payload FROM sample WHERE id > 3 ORDER BY id").fetchall() == [(None,), ("null",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_export_preserves_destination_and_closes_staging(tmp_path, monkeypatch, cancel):
    metadata = MetaData()
    Table("missing", metadata, Column("id", Integer, primary_key=True))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    output = tmp_path / "backup.db"
    output.write_bytes(b"previous backup")
    if cancel:

        async def fail(*args, **kwargs):
            raise asyncio.CancelledError

        # Raise after the source connection is acquired, exercising its cleanup.
        monkeypatch.setattr(db_portable, "_check_export_schema", fail)
    try:
        with pytest.raises(asyncio.CancelledError if cancel else Exception):
            await db_portable._export_pg_to_sqlite(engine, metadata, output)
    finally:
        await engine.dispose()
    assert output.read_bytes() == b"previous backup"
    assert not list(tmp_path.glob(".backup.db.*"))
    output.unlink()  # A leaked SQLite handle fails here on Windows.


def test_restore_preflight_rejects_corruption_and_future_migration(tmp_path):
    path = tmp_path / "backup.db"
    path.write_bytes(b"not SQLite")
    with pytest.raises(ValueError):
        db_portable.validate_sqlite_backup(path)
    path.unlink()
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("CREATE TABLE _migrations(version INTEGER); INSERT INTO _migrations VALUES (999999);")
    with pytest.raises(ValueError, match="newer|unsupported"):
        db_portable.validate_sqlite_backup(path)


@pytest.mark.asyncio
async def test_sqlite_export_includes_uncheckpointed_wal(tmp_path, monkeypatch):
    source, output = tmp_path / "source.db", tmp_path / "snapshot.db"
    monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: True)
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE marker(value TEXT)")
        writer.execute("INSERT INTO marker VALUES ('in the WAL')")
        writer.commit()
        assert source.with_name("source.db-wal").stat().st_size > 0
        engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
        try:
            await db_portable.dump_to_sqlite(engine, MetaData(), output)
        finally:
            await engine.dispose()
    with closing(sqlite3.connect(output)) as db:
        assert db.execute("SELECT * FROM marker").fetchall() == [("in the WAL",)]


@pytest.mark.asyncio
async def test_cancelled_file_work_waits_for_worker_before_cleanup(tmp_path):
    started, finish = threading.Event(), threading.Event()
    path = tmp_path / "worker-finished"

    def worker():
        started.set()
        assert finish.wait(5)
        path.write_text("complete")

    task = asyncio.create_task(db_portable._file_work(worker))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # Repeated cancellation must not detach the worker either.
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert path.read_text() == "complete"


@pytest.mark.asyncio
async def test_constraint_failure_does_not_publish_partial_file(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE sample(id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO sample VALUES (1, NULL);
            CREATE TABLE _migrations(id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE,
                                     name TEXT NOT NULL, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """)
    metadata = MetaData()
    Table("sample", metadata, Column("id", Integer, primary_key=True), Column("name", String, nullable=False))
    output = tmp_path / "backup.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await db_portable._export_pg_to_sqlite(engine, metadata, output)
    finally:
        await engine.dispose()
    assert not output.exists()
    assert not list(tmp_path.glob(".backup.db.*"))
