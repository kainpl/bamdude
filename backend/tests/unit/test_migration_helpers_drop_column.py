"""Dropping a column without hand-writing the table's DDL.

``recreate_table`` needs the full CREATE TABLE of whatever it rewrites. For a
core table that is dozens of columns typed out inside a migration, and the
failure mode is losing a NOT NULL or a DEFAULT that nobody notices until much
later — the same unrecoverable damage the portable backup rule warns about.
SQLite has had ALTER TABLE DROP COLUMN since 3.35; this uses it.
"""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_the_column_is_gone_afterwards(tmp_path):
    from backend.app.migrations.helpers import drop_column, get_table_columns

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT NOT NULL, gone TEXT)"))
        assert await drop_column(conn, "t", "gone") is True
        assert await get_table_columns(conn, "t") == ["id", "keep"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_other_columns_keep_their_constraints(tmp_path):
    """The whole reason for not hand-writing the DDL: a rewritten table quietly
    loses NOT NULL and DEFAULT, and the loss cannot be repaired afterwards."""
    from backend.app.migrations.helpers import drop_column

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT NOT NULL DEFAULT 'x', gone TEXT)"))
        await drop_column(conn, "t", "gone")
        ddl = (await conn.execute(text("SELECT sql FROM sqlite_master WHERE name='t'"))).scalar_one()

    assert "NOT NULL" in ddl
    assert "DEFAULT 'x'" in ddl
    await engine.dispose()


@pytest.mark.asyncio
async def test_dropping_a_column_that_is_not_there_is_not_an_error(tmp_path):
    """DEBUG=true re-runs the newest migration on every start, so this is the
    normal second-run path rather than an edge case."""
    from backend.app.migrations.helpers import drop_column

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        assert await drop_column(conn, "t", "never_existed") is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_data_in_the_kept_columns_survives(tmp_path):
    from backend.app.migrations.helpers import drop_column

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT, gone TEXT)"))
        await conn.execute(text("INSERT INTO t (id, keep, gone) VALUES (1, 'kept', 'dropped')"))
        await drop_column(conn, "t", "gone")
        assert (await conn.execute(text("SELECT keep FROM t WHERE id=1"))).scalar_one() == "kept"
    await engine.dispose()


def test_the_runtime_sqlite_can_actually_do_this():
    """Pinned so an older build is a loud failure here rather than a silent
    no-op inside a migration on somebody's Raspberry Pi."""
    assert tuple(int(part) for part in sqlite3.sqlite_version.split(".")) >= (3, 35, 0)


class TestRecreateTableSurvivesALaterDrop:
    """An old migration's copy list names columns the model no longer has.

    ``recreate_table`` carries a frozen DDL and a frozen column list. When a
    later migration drops one of those columns, a FRESH install breaks: it
    builds the table from today's model and then runs the whole chain from the
    start, so the old migration tries to copy a column that was never there.

    Migrations are frozen, so the old one cannot be edited — which leaves the
    helper. Copying a column the source does not have is never right anyway:
    there is nothing to keep.
    """

    @pytest.mark.asyncio
    async def test_a_column_missing_from_the_source_is_skipped(self, tmp_path):
        from backend.app.migrations.helpers import recreate_table

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        async with engine.begin() as conn:
            # The source lacks "gone" — a later migration removed it from the model.
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT)"))
            await conn.execute(text("INSERT INTO t (id, keep) VALUES (1, 'kept')"))

            await recreate_table(
                conn,
                "t",
                "CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT, gone TEXT)",
                "id, keep, gone",
            )

            assert (await conn.execute(text("SELECT keep FROM t WHERE id=1"))).scalar_one() == "kept"
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_rows_still_come_across(self, tmp_path):
        """The guard must not turn a real copy into a silent truncation."""
        from backend.app.migrations.helpers import recreate_table

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT)"))
            for i in range(5):
                await conn.execute(text("INSERT INTO t (id, keep) VALUES (:i, 'x')"), {"i": i + 1})

            await recreate_table(conn, "t", "CREATE TABLE t (id INTEGER PRIMARY KEY, keep TEXT)", "id, keep")

            assert (await conn.execute(text("SELECT COUNT(*) FROM t"))).scalar_one() == 5
        await engine.dispose()
