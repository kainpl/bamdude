"""m163 — the table behind hidden HMS stack entries exists on an existing
database, with the same shape ``create_all`` gives a fresh one, and running it
twice changes nothing."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m163_hms_muted_entries as m163


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # The parent the FK names; nothing else from the schema is needed.
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
    yield engine
    await engine.dispose()


async def _columns(conn) -> dict[str, tuple[str, int]]:
    rows = (await conn.execute(text("PRAGMA table_info(hms_muted_entries)"))).fetchall()
    return {r[1]: (r[2].upper(), r[3]) for r in rows}  # name -> (type, notnull)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_creates_the_table_with_the_models_shape(engine):
    async with engine.begin() as conn:
        await m163.upgrade(conn)

    async with engine.begin() as conn:
        cols = await _columns(conn)
        assert set(cols) == {"id", "printer_id", "full_code", "created_at"}
        assert cols["printer_id"] == ("INTEGER", 1)
        assert cols["full_code"] == ("VARCHAR(16)", 1)
        assert cols["created_at"][1] == 1

        indexes = (await conn.execute(text("PRAGMA index_list(hms_muted_entries)"))).fetchall()
        names = {row[1] for row in indexes}
        assert "ix_hms_muted_entries_printer_id" in names
        # The unique pair is what makes Hide idempotent at the storage level.
        unique = [row for row in indexes if row[2] == 1]
        assert unique, indexes
        unique_cols = (await conn.execute(text(f"PRAGMA index_info({unique[0][1]})"))).fetchall()
        assert {c[2] for c in unique_cols} == {"printer_id", "full_code"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_pair_is_unique_and_the_second_run_is_a_noop(engine):
    async with engine.begin() as conn:
        await m163.upgrade(conn)
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (7, 'P2S')"))
        await conn.execute(text("INSERT INTO hms_muted_entries (printer_id, full_code) VALUES (7, '0500060000020070')"))

    async with engine.begin() as conn:
        with pytest.raises(Exception, match="UNIQUE"):
            await conn.execute(
                text("INSERT INTO hms_muted_entries (printer_id, full_code) VALUES (7, '0500060000020070')")
            )

    async with engine.begin() as conn:
        await m163.upgrade(conn)  # idempotent: the guard finds the table, the index is IF NOT EXISTS
        count = (await conn.execute(text("SELECT COUNT(*) FROM hms_muted_entries"))).scalar()
        assert count == 1
