"""Regression tests for `import_sqlite_to_postgres` Postgres CASCADE drop path.

Ports upstream Bambuddy's `test_postgres_restore_drop_cascade.py` (audit cycle
v0.2.3.2 → v0.2.4b1, item A.1). Without CASCADE, restoring onto a Postgres
instance carrying orphan FK-bearing tables (e.g. legacy `spoolman_*` whose
constraints still reference `printers`) aborts with
``DependentObjectsStillExistError: cannot drop table printers because other
objects depend on it``.

These tests mock the Postgres engine, run the import path against a tiny
SQLite source, and assert (1) the captured SQL stream contains a CASCADE-aware
iteration over `pg_tables`, and (2) the iteration is scoped to
`schemaname = 'public'` so a shared Postgres instance with non-BamDude data in
other schemas isn't affected.
"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import MetaData, Table

from backend.app.core.db_portable import import_sqlite_to_postgres


def _make_minimal_sqlite(tmp_path: Path) -> Path:
    """Build a tiny SQLite DB with one table so the import path runs through."""
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO printers (id, name) VALUES (1, 'p1')")
    conn.commit()
    conn.close()
    return src


def _capture_sql_engine() -> tuple[MagicMock, list[str]]:
    """Build a mock engine whose conn.execute() captures SQL strings.

    Returns (engine_mock, captured_sql_list). The engine.begin() context
    yields a connection whose .execute() and .run_sync() both append the
    rendered SQL (or callable name) to the list.
    """
    captured: list[str] = []

    conn_mock = MagicMock()

    async def _execute(stmt, *args, **kwargs):
        # SQLAlchemy text() objects render via str()
        captured.append(str(stmt))
        return MagicMock()

    async def _run_sync(fn, *args, **kwargs):
        captured.append(getattr(fn, "__qualname__", repr(fn)))
        return None

    conn_mock.execute = AsyncMock(side_effect=_execute)
    conn_mock.run_sync = AsyncMock(side_effect=_run_sync)

    class _BeginCtx:
        async def __aenter__(self):
            return conn_mock

        async def __aexit__(self, *exc):
            return False

    class _ConnectCtx:
        async def __aenter__(self):
            return conn_mock

        async def __aexit__(self, *exc):
            return False

    engine_mock = MagicMock()
    engine_mock.begin = MagicMock(return_value=_BeginCtx())
    engine_mock.connect = MagicMock(return_value=_ConnectCtx())
    return engine_mock, captured


@pytest.mark.asyncio
async def test_postgres_drop_phase_uses_cascade_iteration(tmp_path):
    """On PG, the drop phase emits a `DROP TABLE … CASCADE` over `pg_tables`.

    Regression guard for item A.1 — if this fails, somebody likely reverted to
    plain `metadata.drop_all`, which on PG re-introduces the orphan-FK abort.
    """
    src = _make_minimal_sqlite(tmp_path)
    engine_mock, captured = _capture_sql_engine()
    metadata = MetaData()
    Table("printers", metadata)

    with patch("backend.app.core.db_dialect.is_postgres", return_value=True):
        # Don't actually iterate `metadata.sorted_tables` for FK-stripping
        # because our minimal MetaData has no FKs anyway. Insert phase will
        # short-circuit on rows={} (the table set is filtered to PG-known names).
        await import_sqlite_to_postgres(engine_mock, metadata, src)

    # Find the DROP TABLE iteration SQL among captured statements.
    drop_sql = next((s for s in captured if "pg_tables" in s and "CASCADE" in s.upper()), None)
    assert drop_sql is not None, (
        f"Expected a CASCADE-aware DROP TABLE iteration over pg_tables; captured statements: {captured!r}"
    )


@pytest.mark.asyncio
async def test_postgres_drop_scoped_to_public_schema(tmp_path):
    """The CASCADE iteration only touches `schemaname = 'public'`.

    A shared Postgres instance might carry non-BamDude data in a different
    schema. The restore must not nuke it.
    """
    src = _make_minimal_sqlite(tmp_path)
    engine_mock, captured = _capture_sql_engine()
    metadata = MetaData()
    Table("printers", metadata)

    with patch("backend.app.core.db_dialect.is_postgres", return_value=True):
        await import_sqlite_to_postgres(engine_mock, metadata, src)

    drop_sql = next(s for s in captured if "pg_tables" in s and "CASCADE" in s.upper())
    assert "schemaname = 'public'" in drop_sql, (
        f"DROP TABLE iteration must filter pg_tables to public schema; got: {drop_sql!r}"
    )


@pytest.mark.asyncio
async def test_sqlite_drop_phase_uses_metadata_drop_all(tmp_path):
    """On SQLite, no CASCADE iteration runs — the legacy ORM path stays."""
    src = _make_minimal_sqlite(tmp_path)
    engine_mock, captured = _capture_sql_engine()
    metadata = MetaData()
    Table("printers", metadata)

    with patch("backend.app.core.db_dialect.is_postgres", return_value=False):
        await import_sqlite_to_postgres(engine_mock, metadata, src)

    # No CASCADE drop should appear; instead we should see metadata.drop_all
    # (recorded as "MetaData.drop_all" via __qualname__).
    cascade_drops = [s for s in captured if "pg_tables" in s and "CASCADE" in s.upper()]
    assert not cascade_drops, f"SQLite path must not run the PG CASCADE iteration; captured: {cascade_drops!r}"
    # Confirm the ORM drop_all path is still hit.
    drop_all_calls = [s for s in captured if "drop_all" in s.lower()]
    assert drop_all_calls, f"Expected metadata.drop_all on SQLite path; captured: {captured!r}"
