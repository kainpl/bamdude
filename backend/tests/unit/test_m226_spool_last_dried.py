"""m226 adds the three «last dried» columns to an existing ``spool`` table."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m226_spool_last_dried as m226


@pytest.mark.asyncio
async def test_m226_adds_the_columns_and_leaves_existing_spools_without_a_record(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm226.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE spool (id INTEGER PRIMARY KEY, material VARCHAR(50), last_used DATETIME)"
        )
        await conn.exec_driver_sql("INSERT INTO spool (id, material) VALUES (1, 'PLA')")

        await m226.upgrade(conn)

        columns = {row[1]: row[2] for row in (await conn.exec_driver_sql("PRAGMA table_info(spool)")).fetchall()}
        assert columns["last_dried_at"] == "DATETIME"
        assert columns["last_dried_temp"] == "INTEGER"
        assert columns["last_dried_hours"] == "FLOAT"
        row = (
            await conn.exec_driver_sql("SELECT last_dried_at, last_dried_temp, last_dried_hours FROM spool")
        ).fetchone()
        assert tuple(row) == (None, None, None)
        # idempotent (DEBUG=true re-runs the newest migration)
        await m226.upgrade(conn)
    await engine.dispose()


def test_the_model_carries_the_same_columns():
    from backend.app.models.spool import Spool

    columns = Spool.__table__.c
    assert str(columns.last_dried_at.type) == "DATETIME"
    assert str(columns.last_dried_temp.type) == "INTEGER"
    assert str(columns.last_dried_hours.type) == "FLOAT"
    assert columns.last_dried_at.nullable and columns.last_dried_temp.nullable and columns.last_dried_hours.nullable


@pytest.mark.asyncio
async def test_postgresql_is_asked_for_a_type_it_has(monkeypatch):
    """PostgreSQL has no DATETIME: an existing database there gets TIMESTAMP, or the upgrade dies at start-up."""
    from types import SimpleNamespace

    asked = []

    async def record(conn, table, column_def):
        asked.append((table, column_def))
        return True

    monkeypatch.setattr(m226, "add_column", record)
    await m226.upgrade(SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    assert ("spool", "last_dried_at TIMESTAMP") in asked
    assert not any("DATETIME" in column_def for _table, column_def in asked)
