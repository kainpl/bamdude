"""m164 creates both tag tables, and running it twice changes nothing."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m164_printer_tags as m164


def test_the_migration_declares_its_version_and_name():
    assert m164.version == 164
    assert m164.name == "printer_tags"


async def _tables(conn) -> set[str]:
    rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    return {r[0] for r in rows}


def _pin_sqlite(monkeypatch) -> None:
    """``is_sqlite()`` reads the app's configured DATABASE_URL, not the engine these
    tests build — pin it, or a PostgreSQL-configured run emits SERIAL into a SQLite file."""
    monkeypatch.setattr(m164, "is_sqlite", lambda: True)


async def test_upgrade_creates_both_tables_and_is_idempotent(tmp_path, monkeypatch):
    _pin_sqlite(monkeypatch)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm164.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY)"))
        await m164.upgrade(conn)
        await m164.upgrade(conn)  # second run: guards hold
        names = await _tables(conn)
        indexes = {r[0] for r in await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}
    await engine.dispose()

    assert {"printer_tags", "printer_tag_links"} <= names
    assert {"ix_printer_tags_name_key", "ix_printer_tag_links_tag"} <= indexes


async def test_the_name_key_index_is_unique(tmp_path, monkeypatch):
    """The backstop behind the route's 409: two tags cannot share a folded name."""
    _pin_sqlite(monkeypatch)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm164-unique.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY)"))
        await m164.upgrade(conn)
        await conn.execute(text("INSERT INTO printer_tags (name, name_key) VALUES ('Фаза 1', 'фаза 1')"))

    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(text("INSERT INTO printer_tags (name, name_key) VALUES ('ФАЗА 1', 'фаза 1')"))
        await conn.rollback()
    await engine.dispose()
