"""The «Optimise database» button, per backend.

⚠️ Verified 2026-09-08: before this fix it ran ANALYZE, then
``PRAGMA wal_checkpoint(TRUNCATE)``, then VACUUM with no dialect branch — so on
PostgreSQL the PRAGMA is a syntax error and the whole call landed in the except.
The button had never once done anything on that backend, while the UI offered
it. On SQLite all three do work through the async engine (measured), so that
path is unchanged.
"""

import pytest

from backend.app.api.routes.settings import optimize_database


class _FakeResult:
    def scalar(self):
        return 0

    def scalar_one(self):
        return 0


class _FakeConn:
    def __init__(self, executed: list[str]):
        self._executed = executed

    async def execute(self, statement):
        self._executed.append(str(statement))
        return _FakeResult()

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, executed: list[str]):
        self._executed = executed

    def connect(self):
        return _FakeConn(self._executed)


@pytest.fixture
def executed(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr("backend.app.core.database.engine", _FakeEngine(statements))
    return statements


def _run_statements(executed: list[str]) -> list[str]:
    return [s.strip().split("(")[0].strip().upper() for s in executed]


@pytest.mark.asyncio
async def test_postgres_runs_analyze_and_no_pragma(monkeypatch, executed):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)
    result = await optimize_database(_=None)

    # ANALYZE, then the size probe — and crucially no PRAGMA and no VACUUM.
    assert _run_statements(executed)[0] == "ANALYZE"
    assert not any("PRAGMA" in s for s in _run_statements(executed))
    assert not any("VACUUM" in s for s in _run_statements(executed))
    assert result["success"] is True
    assert result["mode"] == "postgresql"
    assert "autovacuum" in result["message"]


@pytest.mark.asyncio
async def test_sqlite_still_runs_all_three(monkeypatch, executed):
    monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: False)
    result = await optimize_database(_=None)

    assert _run_statements(executed) == ["ANALYZE", "PRAGMA WAL_CHECKPOINT", "VACUUM"]
    assert result["success"] is True
    assert result["mode"] == "sqlite"
