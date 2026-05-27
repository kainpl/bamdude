"""m084 test — NULL orphaned spool_usage_history.archive_id references.

Past archive deletes left ``spool_usage_history`` rows pointing at a now-gone
``print_archives`` id. The migration severs only those dangling links; rows tied
to a still-present archive (including soft-deleted ones) and rows already
detached are left untouched. The usage rows themselves are always kept.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m084_null_orphaned_usage_history_archive_id as m084


@pytest.mark.asyncio
async def test_m084_nulls_only_dangling_archive_ids():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY, deleted_at TEXT)"))
        await conn.execute(
            text("CREATE TABLE spool_usage_history (id INTEGER PRIMARY KEY, archive_id INTEGER, weight_used REAL)")
        )
        # Archive 1 = live, archive 2 = soft-deleted (still a row). 99 never exists.
        await conn.execute(
            text("INSERT INTO print_archives (id, deleted_at) VALUES (1, NULL), (2, '2026-05-20T00:00:00')")
        )
        await conn.execute(
            text(
                "INSERT INTO spool_usage_history (id, archive_id, weight_used) VALUES "
                "(1, 1, 10.0), "  # → live archive, keep link
                "(2, 2, 20.0), "  # → soft-deleted archive (row exists), keep link
                "(3, 99, 30.0), "  # → dangling, NULL it
                "(4, NULL, 40.0)"  # → already detached, untouched
            )
        )

        await m084.upgrade(conn)
        # Idempotent: a second run matches nothing.
        await m084.upgrade(conn)

        async def archive_id(row_id: int):
            return (
                await conn.execute(text("SELECT archive_id FROM spool_usage_history WHERE id=:i"), {"i": row_id})
            ).scalar()

        assert await archive_id(1) == 1  # live link kept
        assert await archive_id(2) == 2  # soft-deleted link kept
        assert await archive_id(3) is None  # dangling link severed
        assert await archive_id(4) is None  # already detached

        # No usage rows were removed — only the stale link was cleared.
        total = (await conn.execute(text("SELECT COUNT(*) FROM spool_usage_history"))).scalar()
        assert total == 4
    await engine.dispose()
