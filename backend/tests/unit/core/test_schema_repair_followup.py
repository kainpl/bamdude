"""Sanitised failure evidence and nonblocking, transaction-owned residual retry."""

import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import schema_repair
from backend.app.core.schema_repair_state import (
    DEBT,
    RESIDUALS,
    SchemaRepairPending,
    put_record,
    read_record,
    write_pending,
)


@pytest.fixture
async def engine(tmp_path):
    result = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'followup.db'}")
    yield result
    await result.dispose()


@pytest.mark.parametrize("debug", [False, True])
async def test_migration_error_logs_version_phase_and_class_without_values(engine, monkeypatch, caplog, debug):
    from backend.app import migrations
    from backend.app.core.config import settings

    async def fail(_conn):
        raise ValueError("secret row and connection data must not be logged")

    async def applied(_engine):
        return {99} if debug else set()

    monkeypatch.setattr(settings, "debug", debug)
    monkeypatch.setattr(migrations, "_get_applied_versions", applied)
    monkeypatch.setattr(
        migrations,
        "_discover_migrations",
        lambda: [{"version": 99, "name": "test_only", "module": SimpleNamespace(upgrade=fail, replay=fail)}],
    )
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
        await migrations._run_pending(engine, async_sessionmaker(engine))
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        f"Migration 99 failed during {'DEBUG replay' if debug else 'DDL'} (ValueError)"
    ]
    assert all(r.exc_info is None for r in caplog.records)


async def test_repair_error_logs_phase_class_and_preserves_owned_debt(engine, monkeypatch, caplog):
    models = MetaData()
    async with engine.begin() as conn:
        await write_pending(conn, models)
    debt = await schema_repair.claim(engine, models)

    async def fail(*_a, **_k):
        raise OSError("secret source path and driver data must not be logged")

    monkeypatch.setattr(schema_repair, "repair_indexes", fail)
    with caplog.at_level(logging.ERROR), pytest.raises(SchemaRepairPending, match="technical_repair_failure"):
        await schema_repair.run_claimed(engine, models, debt)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        "Import schema repair failed during D4-indexes (OSError)"
    ]
    assert all(r.exc_info is None for r in caplog.records)
    async with engine.connect() as conn:
        assert (await read_record(conn, DEBT))["automatic_attempts_remaining"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        OSError,
        lambda value: SchemaRepairPending("invalid_residuals"),
        lambda value: SchemaRepairPending("unverified_residual_repair"),
    ],
)
async def test_optional_retry_rolls_back_records_and_never_creates_hard_debt(engine, monkeypatch, caplog, failure):
    record = {"version": 1, "entries": {"retained": {"token": "owned"}}}
    async with engine.begin() as conn:
        await put_record(conn, RESIDUALS, record)

    async def fail(conn):
        await put_record(conn, RESIDUALS, {"version": 1, "entries": {}})
        raise failure("secret value")

    monkeypatch.setattr(schema_repair, "retry_index_residuals", fail)
    assert await schema_repair.retry_residuals(engine) is False
    assert "secret value" not in caplog.text
    async with engine.connect() as conn:
        assert await read_record(conn, RESIDUALS) == record
        assert await read_record(conn, DEBT) is None
