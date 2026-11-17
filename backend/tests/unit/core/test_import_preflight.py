"""D4 I2: real SQLite sources, effective PG types and owned single-copy lifetime."""

import asyncio
import io
import json
import sqlite3
import threading
import zipfile
from contextlib import closing
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from backend.app.core import sqlite_import_source as boundary
from backend.app.core.db_portable import prepared_import_source
from backend.app.core.sqlite_import_preflight import SqliteSourceCollisionError, SqliteSourceValuesError


def source_file(path, definition="value VARCHAR(4)", value="ok"):
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE _migrations(version INTEGER)")
        source.execute("INSERT INTO _migrations VALUES(41)")
        source.execute(f"CREATE TABLE sample(id INTEGER PRIMARY KEY, {definition})")
        source.execute("INSERT INTO sample(value) VALUES(?)", (value,))
        source.commit()
    return path


def model(kind):
    metadata = MetaData()
    Table("sample", metadata, Column("id", Integer, primary_key=True), Column("value", kind))
    return metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared,kind,value,reason",
    [
        ("VARCHAR(4)", String(8), "abcdef", None),  # model overrides old source width
        ("VARCHAR(8)", String(4), "abcdef", "over_length"),
        ("VARCHAR(4)", None, "abcdef", "over_length"),  # unknown legacy column
        ("VARCHAR(2)", None, "їя", None),  # characters, not bytes
        ("VARCHAR(2)", None, "їяі", "over_length"),
        ("VARCHAR(2)", None, "ok ", "over_length"),  # PG would silently truncate
        ("VARCHAR(2)", None, None, None),
        ("VARCHAR(2)", None, b"ab", "non_string"),
        ("TEXT", Text(), "a\0b", "nul"),
        ("TEXT", Text(), "a" * 5000, None),
        ("TEXT", JSON(), json.dumps({"private": "a\0b"}), "nul"),
        ("TEXT", JSONB(), json.dumps({"a\0b": [1]}), "nul"),
        ("TEXT", JSON(), '"a\0b"', "nul"),
        ("TEXT", JSONB(), b'{"key":"a\0b"}', "nul"),
        ("JSON", JSON(), "1", None),  # SQLite numeric JSON affinity is not String
        ("TEXT", JSON(), "not JSON", "invalid_json"),
    ],
)
async def test_effective_import_type_and_lossless_text(tmp_path, declared, kind, value, reason):
    path = source_file(tmp_path / "input.db", f"value {declared}", value)
    metadata = model(kind) if kind is not None else MetaData()
    if reason:
        with pytest.raises(SqliteSourceValuesError) as failure:
            async with prepared_import_source(path, metadata):
                pytest.fail("incompatible source reached caller")
        assert failure.value.violations == [{"table": "sample", "column": "value", "reason": reason, "count": 1}]
        assert str(failure.value).startswith("The backup contains text values")
    else:
        async with prepared_import_source(path, metadata) as source:
            assert source.metadata.tables["sample"].c.value.type is not None


@pytest.mark.asyncio
async def test_all_checks_use_one_db_wal_copy_and_never_touch_source(tmp_path, monkeypatch):
    path = tmp_path / "input.db"
    source = sqlite3.connect(path)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.executescript(
            "CREATE TABLE _migrations(version INTEGER); INSERT INTO _migrations VALUES(41);"
            "CREATE TABLE telegram_chats(id INTEGER); CREATE TABLE sample(value VARCHAR(5));"
            "INSERT INTO sample VALUES('hello');"
        )
        source.commit()
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        copies = []
        copy = boundary.shutil.copyfile

        def track(src, dst):
            copies.append(src)
            return copy(src, dst)

        monkeypatch.setattr(boundary.shutil, "copyfile", track)
        async with prepared_import_source(path, MetaData()) as prepared:
            assert boundary.is_legacy_bamdude_source(prepared)
            boundary.validate_postgres_source(prepared)
            with boundary.copied_sqlite_source(prepared) as copied:
                assert copied.execute("SELECT value FROM sample").fetchone() == ("hello",)
            private = prepared.path.parent
            assert len(copies) == 2
            assert private.exists()
        assert not private.exists()
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    finally:
        source.close()


@pytest.mark.asyncio
async def test_late_original_change_is_not_consumable(tmp_path):
    path = source_file(tmp_path / "input.db")
    async with prepared_import_source(path, MetaData()) as prepared:
        with closing(sqlite3.connect(path)) as source:
            source.execute("UPDATE sample SET value='new'")
            source.commit()
        with pytest.raises(ValueError, match="source changed"):
            prepared.assert_unchanged()
        with boundary.copied_sqlite_source(prepared) as frozen:
            assert frozen.execute("SELECT value FROM sample").fetchone() == ("ok",)


@pytest.mark.asyncio
async def test_cancelled_prepare_still_owns_and_removes_the_copy(tmp_path, monkeypatch):
    from backend.app.core import sqlite_import_source

    entered, release = threading.Event(), threading.Event()
    paths = []
    original = sqlite_import_source.prepare_postgres_source

    def blocked(source, metadata):
        paths.append(source.path.parent)
        entered.set()
        assert release.wait(5)
        return original(source, metadata)

    monkeypatch.setattr(sqlite_import_source, "prepare_postgres_source", blocked)

    async def run():
        async with prepared_import_source(source_file(tmp_path / "input.db"), MetaData()):
            pytest.fail("cancelled preparation reached caller")

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paths and not paths[0].exists()


def collision_file(path, index_sql):
    with closing(sqlite3.connect(path)) as source:
        source.executescript(
            "CREATE TABLE _migrations(version INTEGER); INSERT INTO _migrations VALUES(41);"
            "CREATE TABLE user_notifications(id INTEGER PRIMARY KEY, dedupe_key TEXT, other TEXT, "
            "CONSTRAINT uq_user_notifications_dedupe_key UNIQUE(dedupe_key));" + index_sql
        )
        source.commit()
    return path


@pytest.mark.asyncio
async def test_exact_existing_m222_collision_is_omitted_only_in_pg_plan(tmp_path):
    path = collision_file(
        tmp_path / "input.db",
        "CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(dedupe_key);",
    )
    before = path.read_bytes()
    async with prepared_import_source(path, MetaData()) as source:
        table = source.metadata.tables["user_notifications"]
        ddl = str(CreateTable(table).compile(dialect=dialect()))
        ddl += " ".join(str(CreateIndex(i).compile(dialect=dialect())) for i in table.indexes)
        assert ddl.count("uq_user_notifications_dedupe_key") == 1
        assert "UNIQUE (dedupe_key)" in ddl
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        "CREATE INDEX uq_user_notifications_dedupe_key ON user_notifications(dedupe_key)",
        "CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(other)",
        "CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(dedupe_key) WHERE id>0",
        "CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(dedupe_key COLLATE NOCASE)",
        "CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(dedupe_key DESC)",
        "CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(lower(dedupe_key))",
    ],
)
async def test_unknown_collision_never_drops_enforcement(tmp_path, definition):
    path = collision_file(tmp_path / "input.db", definition)
    before = path.read_bytes()
    with pytest.raises(SqliteSourceCollisionError):
        async with prepared_import_source(path, MetaData()):
            pytest.fail("unknown collision accepted")
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_http_length_refusal_precedes_every_drain(tmp_path, monkeypatch):
    from backend.app.api.routes import settings as route
    from backend.app.services.backup_files import FileRestore

    path = source_file(tmp_path / "source.db", value="private-value")
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE printers(id INTEGER); CREATE TABLE settings(key TEXT);")
        db.commit()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.write(path, "bamdude.db")
    stream.seek(0)
    monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: False)
    drain = AsyncMock()
    work = Mock()
    monkeypatch.setattr(route, "_quiesce_part_render", drain)
    monkeypatch.setattr(FileRestore, "prepare", work)
    session = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        await route._restore_backup(UploadFile(file=stream, filename="backup.zip"), session)
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "sqlite_source_text_incompatible"
    assert caught.value.detail["violations"] == [
        {"table": "sample", "column": "value", "reason": "over_length", "count": 1}
    ]
    assert "private-value" not in str(caught.value.detail)
    drain.assert_not_awaited()
    work.assert_not_called()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["constraint", "index", "none"])
async def test_unreleased_m222_preserves_one_existing_full_unique(tmp_path, prior):
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.migrations.m222_auth_lifetime_schema import upgrade

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    try:
        async with engine.begin() as conn:
            clause = ", CONSTRAINT uq_user_notifications_dedupe_key UNIQUE(dedupe_key)" if prior == "constraint" else ""
            await conn.execute(text(f"CREATE TABLE user_notifications(id INTEGER PRIMARY KEY,dedupe_key TEXT{clause})"))
            if prior == "index":
                await conn.execute(text("CREATE UNIQUE INDEX old_full_unique ON user_notifications(dedupe_key)"))
            await upgrade(conn)
            await upgrade(conn)
            unique = await conn.run_sync(lambda c: inspect(c).get_unique_constraints("user_notifications"))
            indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("user_notifications"))
            assert len(unique) + sum(bool(i["unique"]) for i in indexes) == 1
            if prior == "constraint":
                assert not indexes
            if prior == "index":
                assert indexes[0]["name"] == "old_full_unique"
            if prior == "none":
                assert indexes[0]["name"] == "uq_user_notifications_dedupe_key"
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO user_notifications(dedupe_key) VALUES('same')"))
        with pytest.raises(Exception, match="UNIQUE constraint failed"):
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO user_notifications(dedupe_key) VALUES('same')"))
    finally:
        await engine.dispose()
