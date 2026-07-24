"""m107 — print_archives.actual_time_seconds / time_accuracy add + backfill.

Rows are inserted via raw SQL (no ORM, so the before_flush event doesn't populate
the columns) then the migration backfills them; mirrors compute_time_metrics.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m107_archive_actual_time_columns

NEW_COLS = ("actual_time_seconds", "time_accuracy")


@pytest_asyncio.fixture
async def engine_bare_archives():
    """print_archives without the new columns — the pre-m107 schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE print_archives ("
                "id INTEGER PRIMARY KEY, "
                "status VARCHAR(20), "
                "started_at TIMESTAMP, "
                "completed_at TIMESTAMP, "
                "print_time_seconds INTEGER)"
            )
        )
        # id, status, started, completed, print_time_seconds
        rows = [
            (1, "completed", "2026-01-01 10:00:00", "2026-01-01 12:00:00", 7000),  # 7200s real
            (2, "completed", "2026-01-01 10:00:00", "2026-01-01 10:01:40", 1000),  # 100s, ratio 1000% -> NULL acc
            (3, "failed", "2026-01-01 10:00:00", "2026-01-01 10:30:00", 3600),  # not completed
            (4, "printing", "2026-01-01 10:00:00", None, 3600),  # no completed_at
            (5, "completed", "2026-01-01 10:00:00", "2026-01-01 10:00:00", 3600),  # zero duration
        ]
        for r in rows:
            await conn.execute(
                text(
                    "INSERT INTO print_archives "
                    "(id, status, started_at, completed_at, print_time_seconds) "
                    "VALUES (:id, :st, :s, :c, :pt)"
                ),
                {"id": r[0], "st": r[1], "s": r[2], "c": r[3], "pt": r[4]},
            )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _cols(conn) -> set[str]:
    r = await conn.execute(text("PRAGMA table_info(print_archives)"))
    return {row[1] for row in r.fetchall()}


@pytest.mark.asyncio
async def test_m107_adds_columns_and_backfills(engine_bare_archives):
    async with engine_bare_archives.begin() as conn:
        await m107_archive_actual_time_columns.upgrade(conn)

        cols = await _cols(conn)
        assert all(c in cols for c in NEW_COLS)

        r = await conn.execute(text("SELECT id, actual_time_seconds, time_accuracy FROM print_archives ORDER BY id"))
        got = {row[0]: (row[1], row[2]) for row in r.fetchall()}

    assert got[1] == (7200, 97.2)  # round(7000/7200*100, 1)
    assert got[2] == (100, None)  # completed but 1000% accuracy is out of 5..500
    assert got[3] == (None, None)  # failed
    assert got[4] == (None, None)  # printing (no completed_at)
    assert got[5] == (None, None)  # zero duration


@pytest.mark.asyncio
async def test_m107_idempotent(engine_bare_archives):
    async with engine_bare_archives.begin() as conn:
        await m107_archive_actual_time_columns.upgrade(conn)
        await m107_archive_actual_time_columns.upgrade(conn)  # second run must not raise
        r = await conn.execute(text("SELECT actual_time_seconds FROM print_archives WHERE id = 1"))
        assert r.scalar() == 7200
