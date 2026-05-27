"""m083 test — backfill filament_used_grams for failed prints from actual usage.

Failed / partial prints stored the full slicer estimate in
``print_archives.filament_used_grams`` while inventory was deducted by the
*actual* tracked usage recorded in ``spool_usage_history``. The migration
rewrites each tracked failure's total to the sum of what was actually deducted
so the stats page agrees with inventory; completed and untracked prints are
left untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m083_backfill_failed_print_filament as m083


@pytest.mark.asyncio
async def test_m083_backfills_only_tracked_failures():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE print_archives (id INTEGER PRIMARY KEY, status TEXT, filament_used_grams REAL)")
        )
        await conn.execute(
            text("CREATE TABLE spool_usage_history (id INTEGER PRIMARY KEY, archive_id INTEGER, weight_used REAL)")
        )
        await conn.execute(
            text(
                "INSERT INTO print_archives (id, status, filament_used_grams) VALUES "
                "(1, 'failed', 500.0), "  # tracked failure → backfill to actual
                "(2, 'cancelled', 300.0), "  # tracked failure across two spools → 90
                "(3, 'completed', 250.0), "  # completed → untouched even with usage rows
                "(4, 'failed', 400.0), "  # failure with NO usage rows → untouched
                "(5, 'aborted', 120.0)"  # tracked failure → backfill to actual
            )
        )
        await conn.execute(
            text(
                "INSERT INTO spool_usage_history (id, archive_id, weight_used) VALUES "
                "(1, 1, 120.5), "
                "(2, 2, 50.0), (3, 2, 40.0), "  # archive 2 used 2 spools → 90 total
                "(4, 3, 240.0), "  # belongs to a completed print
                "(5, 5, 33.3)"
            )
        )

        await m083.upgrade(conn)
        # Idempotent: re-running must yield the same sums, not double them.
        await m083.upgrade(conn)

        async def grams(archive_id: int) -> float:
            return (
                await conn.execute(
                    text("SELECT filament_used_grams FROM print_archives WHERE id=:i"), {"i": archive_id}
                )
            ).scalar()

        assert await grams(1) == 120.5  # single-spool failure → actual
        assert await grams(2) == 90.0  # multi-spool failure → summed actual
        assert await grams(3) == 250.0  # completed → estimate kept
        assert await grams(4) == 400.0  # untracked failure → estimate kept
        assert await grams(5) == pytest.approx(33.3)  # aborted → actual
    await engine.dispose()
