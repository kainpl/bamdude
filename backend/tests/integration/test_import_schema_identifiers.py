"""Imported identifiers remain data, including during orphan repair and legacy copy.

PostgreSQL cases use only the explicitly supplied disposable TEST_POSTGRES_URL.
"""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import Column, ForeignKeyConstraint, Integer, MetaData, Table, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import db_portable
from backend.app.core.database import Base, import_all_models
from backend.app.migrations.m002_bamdude_311 import upgrade
from backend.tests.integration.test_postgres_scenarios import (
    _async_url,
    _pg_url,
    _serialize_shared_postgres_target as _serialize_shared_postgres_target,
    _wipe,
)


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


@pytest.mark.postgres
@pytest.mark.parametrize("set_null", [False, True])
@pytest.mark.parametrize("odd", ["select", 'name "; DELETE FROM sentinel; --'])
async def test_real_import_repairs_fk_with_literal_identifiers(tmp_path, monkeypatch, set_null, odd):
    url = _pg_url()
    await asyncio.to_thread(_wipe, url)
    source = tmp_path / "source.db"
    parent, child = "parent table", "child table"
    column, key, constraint = odd, "parent key", "foreign_key"
    with closing(sqlite3.connect(source)) as db:
        db.executescript(
            f"CREATE TABLE {quoted(parent)} ({quoted(key)} INTEGER PRIMARY KEY);"
            f"CREATE TABLE {quoted(child)} (id INTEGER PRIMARY KEY, {quoted(column)} INTEGER, "
            f"CONSTRAINT {quoted(constraint)} FOREIGN KEY ({quoted(column)}) "
            f"REFERENCES {quoted(parent)} ({quoted(key)})" + (" ON DELETE SET NULL" if set_null else "") + ");"
            "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TIMESTAMP);"
            "INSERT INTO _migrations VALUES (41, 'fixture', CURRENT_TIMESTAMP);"
            "CREATE TABLE sentinel (value TEXT); INSERT INTO sentinel VALUES ('untouched');"
            f"INSERT INTO {quoted(parent)} VALUES (1);"
            f"INSERT INTO {quoted(child)} VALUES (1, 1), (2, 99), (3, NULL);"
        )
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)
    monkeypatch.setattr(db_portable, "_conform_pending", False)
    engine = create_async_engine(_async_url(url))
    metadata = MetaData()
    Table(parent, metadata, Column(key, Integer, primary_key=True))
    Table(
        child,
        metadata,
        Column("id", Integer, primary_key=True),
        Column(column, Integer),
        ForeignKeyConstraint([column], [metadata.tables[parent].c[key]], ondelete="SET NULL" if set_null else None),
    )
    try:
        await db_portable.import_sqlite_to_postgres(engine, metadata, source)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(f"SELECT * FROM {quoted(child)} ORDER BY id"))).all()
            assert rows == ([(1, 1), (2, None), (3, None)] if set_null else [(1, 1), (3, None)])
            assert (await conn.execute(text("SELECT value FROM sentinel"))).scalar() == "untouched"
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE contype='f' AND conrelid=CAST(:table AS regclass)"),
                    {"table": quoted(child)},
                )
            ).scalar() == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("postgres", [False, True])
@pytest.mark.parametrize("kind", ["config", "logs"])
async def test_legacy_backup_copy_keeps_quoted_shared_columns(tmp_path, monkeypatch, postgres, kind):
    if postgres:
        url = _pg_url()
        await asyncio.to_thread(_wipe, url)
        url = _async_url(url)
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: postgres)
    monkeypatch.setattr("backend.app.migrations.helpers.is_postgres", lambda: postgres)
    import_all_models()
    engine = create_async_engine(url)
    old, new = f"github_backup_{kind}", f"git_backup_{kind}"
    column = 'detail "; DELETE FROM sentinel; --'
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # The importer preserves the source schema, including shared legacy
            # columns not present in today's models. Exercise the real migration.
            await conn.execute(text(f"DROP TABLE {new}" + (" CASCADE" if postgres else "")))
            for table in (old, new):
                await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {quoted(column)} TEXT)"))
            await conn.execute(text(f"INSERT INTO {old} VALUES (1, :value)"), {"value": "preserve me"})
            await conn.execute(text("CREATE TABLE sentinel (value TEXT)"))
            await conn.execute(text("INSERT INTO sentinel VALUES ('untouched')"))
            await upgrade(conn)
            assert (await conn.execute(text(f"SELECT {quoted(column)} FROM {new}"))).scalar() == "preserve me"
            assert (await conn.execute(text("SELECT value FROM sentinel"))).scalar() == "untouched"
    finally:
        await engine.dispose()
