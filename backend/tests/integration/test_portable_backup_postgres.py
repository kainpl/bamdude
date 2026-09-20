"""Destructive ONLY to TEST_POSTGRES_URL, the suite's dedicated scratch database."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import asynccontextmanager, closing

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, event, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from backend.app.core import db_portable
from backend.tests.integration.test_postgres_scenarios import REPO_ROOT, _async_url, _pg_url, _wipe

pytestmark = pytest.mark.postgres


def run(mode, data_dir, url, path):
    env = {**os.environ, "DATA_DIR": str(data_dir), "DEBUG": "false"}
    env.pop("MFA_ENCRYPTION_KEY", None)
    if url:
        env["DATABASE_URL"] = _async_url(url)
    else:
        env.pop("DATABASE_URL", None)
    process = subprocess.run(
        [sys.executable, "-m", "backend.tests.integration.portable_backup_runner", mode, str(path)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert process.returncode == 0, process.stdout[-3000:] + process.stderr[-8000:]
    return json.loads(process.stdout.strip().splitlines()[-1])


def test_postgres_zip_restores_to_sqlite_and_postgres(tmp_path):
    url = _pg_url()
    _wipe(url)
    original = run("export", tmp_path / "source", url, tmp_path)
    zip_path = original.pop("zip")
    assert run("restore-fail", tmp_path / "source", url, zip_path) == {"rollback": True}
    for backend, target in ((None, "sqlite"), (url, "postgresql")):
        restored = run("restore", tmp_path / target, backend, zip_path)
        expected = {**original, "dialect": target}
        assert restored == expected

    # Backwards compatibility for ZIPs created before manifests were added.
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(zip_path) as source, zipfile.ZipFile(legacy, "w") as target:
        for item in source.infolist():
            if item.filename != "backup-manifest.json":
                target.writestr(item, source.read(item))
    restored = run("restore", tmp_path / "legacy-sqlite", None, legacy)
    assert restored == {**original, "dialect": "sqlite"}


@pytest.fixture
def scratch_url():
    url = _pg_url()
    _wipe(url)
    return _async_url(url)


@pytest.mark.asyncio
async def test_export_keeps_one_snapshot_when_writer_commits_between_tables(scratch_url, tmp_path, monkeypatch):
    metadata = MetaData()
    Table("parent", metadata, Column("id", Integer, primary_key=True), Column("value", String))
    Table("child", metadata, Column("id", Integer, primary_key=True), Column("value", String))
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(db_portable._portable_metadata(metadata).create_all)
            from backend.app.migrations import _discover_migrations

            await conn.execute(
                text("INSERT INTO _migrations(version, name) VALUES (:version, :name)"),
                [{"version": m["version"], "name": m["name"]} for m in _discover_migrations()],
            )
            await conn.execute(text("INSERT INTO parent VALUES (1, 'before')"))
            await conn.execute(text("INSERT INTO child VALUES (1, 'before')"))
        original = AsyncConnection.stream
        writes = []

        @asynccontextmanager
        async def stream(connection, statement, *args, **kwargs):
            async with original(connection, statement, *args, **kwargs) as result:
                yield result
            if "FROM parent" in str(statement):
                async with engine.begin() as writer:
                    await writer.execute(text("UPDATE parent SET value='after'"))
                    await writer.execute(text("UPDATE child SET value='after'"))
                    writes.append(True)

        monkeypatch.setattr(AsyncConnection, "stream", stream)
        output = tmp_path / "snapshot.db"
        await db_portable._export_pg_to_sqlite(engine, metadata, output)
        assert writes == [True]
        with closing(sqlite3.connect(output)) as db:
            assert db.execute("SELECT value FROM parent").fetchone() == ("before",)
            assert db.execute("SELECT value FROM child").fetchone() == ("before",)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT value FROM child"))).scalar() == "after"
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM _migrations WHERE version=(SELECT MAX(version) FROM _migrations)"))
        previous = output.read_bytes()
        with pytest.raises(ValueError, match="Finish database migrations"):
            await db_portable._export_pg_to_sqlite(engine, metadata, output)
        assert output.read_bytes() == previous
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["load", "foreign_key", "cancel"])
async def test_failed_import_rolls_back_old_database(scratch_url, tmp_path, monkeypatch, phase):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript("""
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));
            INSERT INTO parent VALUES (1);
            INSERT INTO child VALUES (2, 1);
        """)
    metadata = MetaData()
    Table("parent", metadata, Column("id", Integer, primary_key=True))
    Table("child", metadata, Column("id", Integer, primary_key=True), Column("parent_id", ForeignKey("parent.id")))
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)
    monkeypatch.setattr(db_portable, "_conform_pending", False)
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE original(id SERIAL PRIMARY KEY, value TEXT NOT NULL)"))
            await conn.execute(text("INSERT INTO original(value) VALUES ('must survive')"))

        def fail(conn, cursor, statement, parameters, context, executemany):
            if (phase == "foreign_key" and " ADD CONSTRAINT " in statement) or (
                phase != "foreign_key" and 'INSERT INTO "child"' in statement
            ):
                raise asyncio.CancelledError if phase == "cancel" else RuntimeError("injected import failure")

        event.listen(engine.sync_engine, "before_cursor_execute", fail)
        with pytest.raises(asyncio.CancelledError if phase == "cancel" else RuntimeError):
            await db_portable.import_sqlite_to_postgres(engine, metadata, source)
        event.remove(engine.sync_engine, "before_cursor_execute", fail)
        assert db_portable._conform_pending is False
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT * FROM original"))).all() == [(1, "must survive")]
            assert (await conn.execute(text("SELECT to_regclass('child')"))).scalar() is None
            assert (await conn.execute(text("INSERT INTO original(value) VALUES ('next') RETURNING id"))).scalar() == 2
        source.unlink()  # Proves the source handle closed on Windows, too.
    finally:
        await engine.dispose()
