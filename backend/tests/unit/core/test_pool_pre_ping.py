"""Pre-ping is a PG opt-out, preserving the default and SQLite behavior."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from backend.app.core import database
from backend.app.core.config import Settings, settings


@pytest.fixture(autouse=True)
def clean_pool_settings(monkeypatch):
    monkeypatch.delenv("DB_POOL_PRE_PING", raising=False)
    for name in (
        "db_pool_pre_ping",
        "db_pool_size",
        "db_max_overflow",
        "db_pool_timeout",
        "db_pool_recycle",
        "db_pool_use_lifo",
    ):
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(database, "_pool_config", {})


@pytest.mark.parametrize(
    "raw,expected",
    [(None, None), ("true", True), ("false", False), ("1", True), ("0", False), ("yes", True), ("no", False)],
)
def test_environment_parses_optional_bool(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("DB_POOL_PRE_PING", raw)
    assert Settings(_env_file=None).db_pool_pre_ping is expected


@pytest.mark.parametrize("raw", ["not-a-bool", "null", ""])
def test_invalid_environment_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("DB_POOL_PRE_PING", raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("configured,expected", [(None, True), (True, True), (False, False)])
async def test_pg_engine_and_diagnostic_use_resolved_bool(monkeypatch, configured, expected):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    monkeypatch.setattr(settings, "db_pool_pre_ping", configured)
    monkeypatch.setattr(database, "is_sqlite", lambda: False)
    engine = database._create_engine()
    monkeypatch.setattr(database, "engine", engine)
    try:
        assert database._resolve_pool_kwargs()["pool_pre_ping"] is expected
        assert engine.pool._pre_ping is expected
        status = database.get_pool_status()
        assert status["config"]["pool_pre_ping"] is expected
        assert status["checked_out"] == 0
        assert (status["config"]["pool_size"], status["config"]["max_overflow"]) == (20, 60)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("configured", [None, True, False])
async def test_sqlite_ignores_setting(monkeypatch, tmp_path, configured):
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'ping.sqlite'}")
    monkeypatch.setattr(settings, "db_pool_pre_ping", configured)
    monkeypatch.setattr(database, "is_sqlite", lambda: True)
    assert database._resolve_pool_kwargs() == {"pool_size": 20, "max_overflow": 200}
    engine = database._create_engine()
    monkeypatch.setattr(database, "engine", engine)
    try:
        assert engine.pool._pre_ping is False
        assert database.get_pool_status()["config"]["pool_pre_ping"] is False
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT lower('Ж')")) == "ж"
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 15000
    finally:
        await engine.dispose()


@pytest.mark.parametrize("configured", [True, False])
async def test_reinitialize_preserves_explicit_setting(monkeypatch, configured):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    monkeypatch.setattr(settings, "db_pool_pre_ping", configured)
    monkeypatch.setattr(database, "is_sqlite", lambda: False)
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "async_session", None)
    probe = AsyncMock()
    monkeypatch.setattr(database.case_folding, "probe_postgres_case_folding", probe)
    await database.reinitialize_database()
    try:
        assert database.engine.pool._pre_ping is configured
        assert database.get_pool_status()["config"]["pool_pre_ping"] is configured
        probe.assert_awaited_once_with(database.engine)
    finally:
        await database.engine.dispose()
