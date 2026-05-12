"""Smoke test for m060 — creates ams_setting_audit table + index, idempotent."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m060_ams_setting_audit


@pytest_asyncio.fixture
async def engine_with_prereqs():
    """In-memory SQLite with minimal printers + users tables (FK targets)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def _table_exists(conn, name: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    )
    return result.scalar() is not None


async def _index_exists(conn, name: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": name},
    )
    return result.scalar() is not None


@pytest.mark.asyncio
async def test_m060_creates_table_and_index(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m060_ams_setting_audit.upgrade(conn)
        assert await _table_exists(conn, "ams_setting_audit")
        assert await _index_exists(conn, "ix_ams_setting_audit_printer")


@pytest.mark.asyncio
async def test_m060_is_idempotent(engine_with_prereqs):
    async with engine_with_prereqs.begin() as conn:
        await m060_ams_setting_audit.upgrade(conn)
        # Second run must not raise.
        await m060_ams_setting_audit.upgrade(conn)
        assert await _table_exists(conn, "ams_setting_audit")
