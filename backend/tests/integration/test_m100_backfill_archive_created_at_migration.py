"""m100 test — backfill NULL print_archives.created_at (upstream #1732 / M3).

Legacy rows (bambuddy.db rename, cross-DB restore) can carry created_at=NULL,
which 500'd GET /archives. The migration sets created_at to the best available
timestamp: COALESCE(completed_at, started_at, now()). Non-NULL rows are left
untouched (WHERE created_at IS NULL), so it's idempotent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m100_backfill_archive_created_at as m100


@pytest.mark.asyncio
async def test_m100_backfills_only_null_created_at():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE print_archives ("
                "id INTEGER PRIMARY KEY, created_at TEXT, completed_at TEXT, started_at TEXT)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO print_archives (id, created_at, completed_at, started_at) VALUES "
                # 1: NULL created_at → backfill from completed_at
                "(1, NULL, '2026-05-20T10:00:00', '2026-05-20T09:00:00'), "
                # 2: NULL created_at, NULL completed_at → backfill from started_at
                "(2, NULL, NULL, '2026-05-21T08:00:00'), "
                # 3: NULL created_at, both NULL → backfill from now()
                "(3, NULL, NULL, NULL), "
                # 4: created_at already set → untouched
                "(4, '2026-01-01T00:00:00', '2026-05-22T00:00:00', NULL)"
            )
        )

        await m100.upgrade(conn)
        # Idempotent: a second run matches nothing (WHERE created_at IS NULL).
        await m100.upgrade(conn)

        async def created_at(row_id: int):
            return (
                await conn.execute(text("SELECT created_at FROM print_archives WHERE id=:i"), {"i": row_id})
            ).scalar()

        # No NULLs remain — the list endpoint can't 500 on this table anymore.
        remaining_nulls = (
            await conn.execute(text("SELECT COUNT(*) FROM print_archives WHERE created_at IS NULL"))
        ).scalar()
        assert remaining_nulls == 0

        assert await created_at(1) == "2026-05-20T10:00:00"  # completed_at wins
        assert await created_at(2) == "2026-05-21T08:00:00"  # falls back to started_at
        assert await created_at(3) is not None  # datetime('now') literal
        assert await created_at(4) == "2026-01-01T00:00:00"  # pre-existing value untouched

    await engine.dispose()
