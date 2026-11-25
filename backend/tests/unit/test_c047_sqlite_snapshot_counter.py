"""C047 spec §B: a SQLite backup copies pages, ``sqlite_sequence`` with them — WAL included."""

import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import db_portable


@pytest.mark.asyncio
async def test_the_sqlite_snapshot_keeps_the_counter_of_an_emptied_newest_row(tmp_path, monkeypatch):
    source = tmp_path / "live.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")  # the counter lives only in the WAL when copied
    writer.execute("CREATE TABLE spool (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, material VARCHAR(50))")
    writer.executemany("INSERT INTO spool (material) VALUES (?)", [("PLA",), ("PETG",), ("ABS",)])
    writer.execute("DELETE FROM spool WHERE id = 3")
    writer.commit()

    metadata = MetaData()
    Table(
        "spool",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("material", String(50)),
        sqlite_autoincrement=True,
    )
    monkeypatch.setattr(db_portable, "_annotate_sqlite_snapshot", lambda *_a, **_k: _noop())
    engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    copy = tmp_path / "copy.db"
    try:
        monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: True)
        await db_portable.dump_to_sqlite(engine, metadata, copy)
    finally:
        await engine.dispose()
        writer.close()
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'spool'").fetchone()[0] == 3
        db.execute("INSERT INTO spool (material) VALUES ('TPU')")
        assert db.execute("SELECT MAX(id) FROM spool").fetchone()[0] == 4


async def _noop():
    return None
