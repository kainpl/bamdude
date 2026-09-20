"""``recreate_table`` carries the old table's indexes over on SQLite.

The copy-drop-rename rebuild used to lose every index the DDL did not restate:
a migration that recreated ``auto_queue_items`` silently dropped
``ix_auto_queue_status_position`` from m024, and long-lived installs ran
without four indexes a fresh install had (m172 puts those back). Now the
helper reads the table's own indexes from ``sqlite_master`` before the drop and
recreates the ones whose columns survived; an index on a dropped column is
left behind on purpose, and an auto-index (UNIQUE / PRIMARY KEY, ``sql`` NULL)
is the new DDL's business.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations.helpers import recreate_table

_OLD = "CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT, gone TEXT, code TEXT UNIQUE)"
_NEW = "CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT, code TEXT UNIQUE)"


async def _indexes(conn) -> dict[str, str | None]:
    rows = await conn.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 't'"))
    return {r[0]: r[1] for r in rows.fetchall()}


@pytest.mark.asyncio
async def test_indexes_on_surviving_columns_are_kept_and_the_dropped_columns_are_not(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with engine.begin() as conn:
        await conn.execute(text(_OLD))
        await conn.execute(text("CREATE INDEX ix_t_keep ON t (keep)"))
        await conn.execute(text("CREATE UNIQUE INDEX ux_t_keep_code ON t (keep, code)"))
        await conn.execute(text("CREATE INDEX ix_t_gone ON t (gone)"))
        await conn.execute(text("INSERT INTO t (keep, gone, code) VALUES ('a', 'x', 'c1')"))

        await recreate_table(conn, "t", _NEW, "id, keep, code")

        after = await _indexes(conn)
        rows = (await conn.execute(text("SELECT keep, code FROM t"))).fetchall()
    await engine.dispose()

    assert "ix_t_keep" in after
    assert "ux_t_keep_code" in after and "UNIQUE" in (after["ux_t_keep_code"] or "")
    assert "ix_t_gone" not in after
    assert rows == [("a", "c1")]


@pytest.mark.asyncio
async def test_an_index_the_new_ddl_would_recreate_is_not_duplicated(tmp_path):
    """The migration that calls the helper may also (re)create an index by the
    same name afterwards with IF NOT EXISTS — the carry-over must not make that
    raise, and must not create two."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    async with engine.begin() as conn:
        await conn.execute(text(_OLD))
        await conn.execute(text("CREATE INDEX ix_t_keep ON t (keep)"))
        await recreate_table(conn, "t", _NEW, "id, keep, code")
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_t_keep ON t (keep)"))
        after = await _indexes(conn)
    await engine.dispose()

    assert list(after).count("ix_t_keep") == 1
