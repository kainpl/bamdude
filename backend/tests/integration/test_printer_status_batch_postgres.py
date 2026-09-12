"""The fleet snapshot queries run on a disposable PostgreSQL as well as SQLite."""

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.services import embedded_postgres as ep
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch import (
    test_fifty_statuses_match_single_reads_with_bounded_sql as assert_fleet_parity,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("embedded_postgres")


@pytest.fixture
async def test_engine(live_settings):
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
        import_all_models()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        await ep.stop()


async def test_fifty_printer_snapshot_on_postgres(printer_factory, archive_factory, db_session):
    await assert_fleet_parity(printer_factory, archive_factory, db_session)
