import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import (
    m043_print_options_preferences as m043,
    m127_sensor_thresholds as m127,
    m221_active_print_session_owner as m221,
    m231_postgres_historical_schema as m228,
)
from backend.app.migrations.helpers import _to_postgres_column_def


@pytest.mark.parametrize(
    "definition,expected",
    [
        ("stamp DATETIME", "stamp TIMESTAMP"),
        ("data BLOB", "data BYTEA"),
        (" stamp datetime DEFAULT 'DATETIME BLOB'", " stamp TIMESTAMP DEFAULT 'DATETIME BLOB'"),
        ("other TEXT DEFAULT 'DATETIME'", "other TEXT DEFAULT 'DATETIME'"),
        ("enabled BOOLEAN DEFAULT 1", "enabled BOOLEAN DEFAULT true"),
        ("item INTEGER PRIMARY KEY", "item SERIAL PRIMARY KEY"),
    ],
)
def test_type_token_only(definition, expected):
    assert _to_postgres_column_def(definition) == expected


@pytest.mark.parametrize("module,table", [(m043, "print_options_preferences"), (m127, "smart_sensor_thresholds")])
@pytest.mark.parametrize("postgres", [True, False])
@pytest.mark.asyncio
async def test_rendered_old_sqlite_is_exact_and_missing_table_pg_is_portable(monkeypatch, module, table, postgres):
    monkeypatch.setattr(module, "table_exists", AsyncMock(return_value=False))
    if module is m043:
        monkeypatch.setattr(module, "is_postgres", lambda: postgres)
    else:
        monkeypatch.setattr(module, "is_sqlite", lambda: not postgres)
        monkeypatch.setattr(module, "add_column", AsyncMock())
    statements = []

    async def execute(sql):
        statements.append(str(sql))

    conn = AsyncMock()
    conn.execute = execute
    await module.upgrade(conn)
    ddl = next(s for s in statements if "CREATE TABLE " + table in s)
    if postgres:
        assert "AUTOINCREMENT" not in ddl and "DATETIME" not in ddl
        if module is m127:
            assert "DEFAULT TRUE" in ddl
    else:
        frozen = Path(__file__).parents[1] / "integration/fixtures/g02_pg_schema/sqlite-ddl.json"
        assert ddl == json.loads(frozen.read_text())[module.__name__.split(".")[-1]]


@pytest.mark.asyncio
async def test_m221_missing_table_and_existing_owner_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(m221, "is_postgres", lambda: False)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "owner.db"))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("CREATE TABLE printers(id INTEGER PRIMARY KEY)"))
            await conn.execute(text("INSERT INTO printers VALUES(1)"))
            await m221.upgrade(conn)
            assert await conn.scalar(text("SELECT count(*) FROM active_print_sessions")) == 0
            await conn.execute(
                text(
                    "INSERT INTO active_print_sessions(printer_id,print_name,started_at,tray_now_at_start) VALUES(1,'existing','2026-01-01',255)"
                )
            )
            await m221.upgrade(conn)
            row = (
                await conn.execute(text("SELECT print_name,archive_identity,owner_tracked FROM active_print_sessions"))
            ).one()
            assert tuple(row) == ("existing", None, None)
            fk = (await conn.execute(text("PRAGMA foreign_key_list(active_print_sessions)"))).one()
            assert fk[6] == "CASCADE"
            await conn.execute(text("DELETE FROM printers WHERE id=1"))
            assert await conn.scalar(text("SELECT count(*) FROM active_print_sessions")) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pg_repair_is_noop_on_sqlite(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "noop.db"))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE untouched (value TEXT)"))
            await conn.execute(text("INSERT INTO untouched VALUES ('before')"))
            before = (await conn.execute(text("SELECT sql FROM sqlite_master"))).all()
            await m228.upgrade(conn)
            await m228.replay(conn)
            assert (await conn.execute(text("SELECT sql FROM sqlite_master"))).all() == before
            assert await conn.scalar(text("SELECT value FROM untouched")) == "before"
    finally:
        await engine.dispose()
