"""Migration proof on a disposable bundled PostgreSQL, never the operator database."""

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import settings
from backend.app.services import embedded_postgres as ep
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.unit.services.test_filament_policy import assert_migration_contract

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("embedded_postgres")


async def test_routing_migration_postgres_backfill_and_repeat(live_settings, monkeypatch):
    monkeypatch.setattr("backend.app.migrations.helpers.is_postgres", lambda: True)
    engine = None
    try:
        await ep.start()
        engine = create_async_engine(
            URL.create(
                "postgresql+asyncpg",
                username=ep.PG_USER,
                password="live-test-password",
                host=ep.PG_HOST,
                port=settings.embedded_pg_port,
                database=ep.PG_DATABASE,
            )
        )
        async with engine.begin() as conn:
            await assert_migration_contract(conn)
    finally:
        if engine is not None:
            await engine.dispose()
        await ep.stop()
