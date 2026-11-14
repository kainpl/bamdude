"""The default pool leaves server headroom without restricting explicit overrides."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text

from backend.app.core import database
from backend.app.core.config import Settings, settings


@pytest.fixture(autouse=True)
def default_pool_settings(monkeypatch):
    for field in (
        "db_pool_size",
        "db_max_overflow",
        "db_pool_timeout",
        "db_pool_recycle",
        "db_pool_use_lifo",
        "db_pool_pre_ping",
    ):
        monkeypatch.setattr(settings, field, None)
    monkeypatch.setattr(database, "_pool_config", {})


def test_postgres_default_and_connection_options(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    options = database._resolve_pool_kwargs()
    assert options == {
        "pool_size": 20,
        "max_overflow": 60,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_use_lifo": True,
    }
    assert database._resolve_connect_args() == {"server_settings": {"timezone": "UTC"}}


@pytest.mark.parametrize("size,overflow", [(0, -1), (7, 0), (20, 80), (50, 200)])
def test_explicit_pool_settings_survive_validation_and_resolution(monkeypatch, size, overflow):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    parsed = Settings(_env_file=None, db_pool_size=size, db_max_overflow=overflow)
    monkeypatch.setattr(settings, "db_pool_size", parsed.db_pool_size)
    monkeypatch.setattr(settings, "db_max_overflow", parsed.db_max_overflow)
    monkeypatch.setattr(settings, "db_pool_timeout", 4)
    monkeypatch.setattr(settings, "db_pool_recycle", 0)
    monkeypatch.setattr(settings, "db_pool_use_lifo", False)
    options = database._resolve_pool_kwargs()
    assert (options["pool_size"], options["max_overflow"]) == (size, overflow)
    assert (options["pool_timeout"], options["pool_recycle"], options["pool_use_lifo"]) == (4, 0, False)


async def test_real_engines_have_independent_capacity_and_diagnostics_do_not_connect(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    first, second = database._create_engine(), database._create_engine()
    try:
        assert first.pool is not second.pool
        for engine in (first, second):
            assert engine.pool.size() == 20
            assert engine.pool._max_overflow == 60
            assert event.contains(engine.sync_engine, "before_cursor_execute", database._strip_tz_from_params)
        monkeypatch.setattr(database, "engine", second)
        monkeypatch.setattr(
            type(second.pool), "connect", lambda self: pytest.fail("diagnostics checked out a connection")
        )
        snapshot = database.get_pool_status()
        assert snapshot["config"]["max_overflow"] == 60
        assert snapshot["checked_out"] == 0
        snapshot["config"]["max_overflow"] = 999
        assert database.get_pool_status()["config"]["max_overflow"] == 60
    finally:
        await first.dispose()
        await second.dispose()


async def test_reinitialize_uses_new_default_and_preserves_case_folding_probe(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "async_session", None)
    probe = AsyncMock()
    monkeypatch.setattr(database.case_folding, "probe_postgres_case_folding", probe)
    await database.reinitialize_database()
    try:
        assert database.engine.pool._max_overflow == 60
        assert database.async_session.kw["bind"] is database.engine
        probe.assert_awaited_once_with(database.engine)
    finally:
        await database.engine.dispose()


async def test_sqlite_keeps_defaults_and_connection_listener(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'pool.sqlite'}")
    assert database._resolve_pool_kwargs() == {"pool_size": 20, "max_overflow": 200}
    assert database._resolve_connect_args() == {}
    engine = database._create_engine()
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 15000
            assert await connection.scalar(text("SELECT lower('Ж')")) == "ж"
    finally:
        await engine.dispose()
