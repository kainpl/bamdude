"""Smoke test for m159 — the forecast engine's (spool_id, created_at) index."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m159_forecast_usage_index


@pytest_asyncio.fixture
async def engine_with_table():
    """In-memory SQLite with a minimal spool_usage_history (the index target)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE spool_usage_history ("
                "id INTEGER PRIMARY KEY, spool_id INTEGER, weight_used FLOAT, created_at DATETIME)"
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


@pytest.mark.asyncio
async def test_m159_creates_the_index(engine_with_table):
    async with engine_with_table.begin() as conn:
        await m159_forecast_usage_index.upgrade(conn)
        assert await _index_exists(conn, "ix_spool_usage_history_spool_created")


@pytest.mark.asyncio
async def test_m159_is_idempotent(engine_with_table):
    async with engine_with_table.begin() as conn:
        await m159_forecast_usage_index.upgrade(conn)
        # Second run must not raise (IF NOT EXISTS on both dialects).
        await m159_forecast_usage_index.upgrade(conn)
        assert await _index_exists(conn, "ix_spool_usage_history_spool_created")
