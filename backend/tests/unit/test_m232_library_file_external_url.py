"""m232 adds ``library_files.external_url`` to an existing table."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m232_library_file_external_url as m232


@pytest.mark.asyncio
async def test_m232_adds_the_column_and_leaves_existing_files_without_a_link(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm232.db'}")
    async with engine.begin() as conn:
        # A table from before m231: the legacy ``notes`` column is still there, and
        # this migration neither reads nor needs it.
        await conn.exec_driver_sql(
            "CREATE TABLE library_files (id INTEGER PRIMARY KEY, filename VARCHAR(255), notes TEXT, source_url VARCHAR(512))"
        )
        await conn.exec_driver_sql("INSERT INTO library_files (id, filename, notes) VALUES (1, 'a.3mf', 'old')")

        await m232.upgrade(conn)

        columns = {
            row[1]: row[2] for row in (await conn.exec_driver_sql("PRAGMA table_info(library_files)")).fetchall()
        }
        assert columns["external_url"] == "VARCHAR(2048)"
        row = (await conn.exec_driver_sql("SELECT external_url, notes FROM library_files")).fetchone()
        assert tuple(row) == (None, "old")
        # idempotent (DEBUG=true re-runs the newest migration)
        await m232.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_m232_works_on_a_table_without_the_legacy_notes_column(tmp_path):
    """Another branch's m231 removes ``library_files.notes`` from the API; whatever
    it leaves of the column, this migration does not depend on it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm232b.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE library_files (id INTEGER PRIMARY KEY, filename VARCHAR(255))")
        await m232.upgrade(conn)
        columns = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(library_files)")).fetchall()}
        assert "external_url" in columns
    await engine.dispose()


def test_the_model_carries_the_same_column():
    from backend.app.models.library import LibraryFile

    column = LibraryFile.__table__.c.external_url
    assert str(column.type) == "VARCHAR(2048)"
    assert column.nullable
