"""Regression test for m025_settings_dedupe.

Simulates a legacy SQLite settings table without the unique index, inserts
duplicate rows for the same key, runs the migration, and asserts:
- duplicates collapsed to MIN(id) per key,
- the unique index now exists,
- a fresh run is a no-op on an already-clean table.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m025_settings_dedupe


@pytest_asyncio.fixture
async def legacy_engine():
    """Engine with a settings table missing the unique index — pre-fork shape."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Plain table with a non-unique key column — what early Bambuddy
        # installs landed with before the constraint was added.
        await conn.execute(
            text(
                """
                CREATE TABLE settings (
                    id INTEGER PRIMARY KEY,
                    key VARCHAR(100) NOT NULL,
                    value TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _index_exists(conn, name: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": name},
    )
    return result.scalar() is not None


async def _all_rows(conn) -> list[tuple]:
    result = await conn.execute(text("SELECT id, key, value FROM settings ORDER BY id"))
    return [(r[0], r[1], r[2]) for r in result.fetchall()]


@pytest.mark.asyncio
async def test_dedupe_keeps_min_id_per_key(legacy_engine):
    async with legacy_engine.begin() as conn:
        # 3 rows for "language", 2 rows for "theme", 1 unique row.
        await conn.execute(
            text(
                "INSERT INTO settings (id, key, value) VALUES "
                "(1, 'language', 'en'), "
                "(2, 'language', 'uk'), "  # duplicate
                "(3, 'language', 'de'), "  # duplicate
                "(4, 'theme', 'dark'), "
                "(5, 'theme', 'light'), "  # duplicate
                "(6, 'frame_color', '#ff0000')"
            )
        )

    async with legacy_engine.begin() as conn:
        await m025_settings_dedupe.upgrade(conn)

    async with legacy_engine.begin() as conn:
        rows = await _all_rows(conn)
        # MIN(id) wins: id=1 (language=en), id=4 (theme=dark), id=6 (frame_color)
        assert rows == [
            (1, "language", "en"),
            (4, "theme", "dark"),
            (6, "frame_color", "#ff0000"),
        ]
        assert await _index_exists(conn, "ix_settings_key")


@pytest.mark.asyncio
async def test_idempotent_on_clean_table(legacy_engine):
    """Already-deduped table — migration is a no-op except for index creation."""
    async with legacy_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (id, key, value) VALUES (1, 'language', 'en'), (2, 'theme', 'dark')")
        )

    async with legacy_engine.begin() as conn:
        await m025_settings_dedupe.upgrade(conn)
    async with legacy_engine.begin() as conn:
        await m025_settings_dedupe.upgrade(conn)  # second run

    async with legacy_engine.begin() as conn:
        assert await _all_rows(conn) == [(1, "language", "en"), (2, "theme", "dark")]
        assert await _index_exists(conn, "ix_settings_key")


@pytest.mark.asyncio
async def test_unique_index_blocks_future_duplicates(legacy_engine):
    async with legacy_engine.begin() as conn:
        await conn.execute(text("INSERT INTO settings (id, key, value) VALUES (1, 'language', 'en')"))
        await m025_settings_dedupe.upgrade(conn)

    # After the migration, INSERT of a duplicate key must raise IntegrityError.
    async with legacy_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(text("INSERT INTO settings (id, key, value) VALUES (2, 'language', 'uk')"))
