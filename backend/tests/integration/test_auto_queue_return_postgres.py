"""Foundation constraints and portable SQLite -> real native PG -> SQLite."""

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import db_portable
from backend.app.core.config import settings
from backend.app.core.database import Base
from backend.app.migrations import m226_auto_queue_return_facts as migration
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.tests.integration.test_auto_queue_return_portable import assert_retained, seed_file
from backend.tests.integration.test_auto_queue_return_schema import (
    STAMP,
    assert_unique_race,
    assert_uniqueness,
    legacy_schema,
    receipt_values,
)
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def dialect(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))


async def test_return_uniqueness_on_postgres(test_engine):
    await assert_uniqueness(test_engine)


@pytest.mark.parametrize("key", ["attempt", "ordinal"])
async def test_receipt_race_on_postgres(test_engine, key):
    await assert_unique_race(test_engine, key)


@pytest.mark.parametrize("returned,ordinal", [(None, 1), (STAMP, None), (STAMP, 0), (STAMP, -1)])
async def test_half_publication_refused_on_postgres(test_engine, returned, ordinal):
    with pytest.raises(IntegrityError):
        async with test_engine.begin() as conn:
            await conn.execute(insert(AutoQueueReturn), receipt_values(returned_at=returned, ordinal=ordinal))


async def test_existing_postgres_upgrades_without_legacy_proofs(test_engine):
    async with test_engine.begin() as conn:
        names = ", ".join(conn.dialect.identifier_preparer.quote(t.name) for t in Base.metadata.tables.values())
        await conn.execute(text(f"DROP TABLE IF EXISTS {names} CASCADE"))
        await legacy_schema(conn)
        for _ in range(2):
            await migration.upgrade(conn)
        assert await conn.scalar(text("SELECT failed_return_count FROM auto_queue_items")) == 0
        assert await conn.scalar(text("SELECT failed_return_proof FROM auto_queue_items")) is None
        assert await conn.scalar(text("SELECT returned_receipt_identity FROM print_queue")) is None
        assert await conn.scalar(text("SELECT count(*) FROM auto_queue_returns")) == 0
        assert await conn.scalar(text("SELECT error_code FROM print_queue")) == "physical_failure"
    with pytest.raises(IntegrityError):
        async with test_engine.begin() as conn:
            await conn.execute(text("UPDATE auto_queue_items SET failed_return_count=-1"))


async def test_actual_sqlite_postgres_portable_roundtrip(test_engine, tmp_path):
    source, restored = tmp_path / "sqlite-source.db", tmp_path / "portable-restored.db"
    returned, ready, proof = await seed_file(source)
    imported = await db_portable.import_sqlite_to_postgres(test_engine, Base.metadata, source)
    assert imported >= 4
    async with test_engine.begin() as conn:
        await migration.upgrade(conn)
        assert await conn.scalar(text("SELECT value FROM settings WHERE key='auto_queue_return_failed'")) == "true"
    await assert_retained(test_engine, returned, ready, proof)
    # Constraint/index name collision would fail the real import before this.
    with pytest.raises(IntegrityError):
        async with test_engine.begin() as conn:
            await conn.execute(
                insert(AutoQueueReturn),
                receipt_values(auto_identity=returned["auto_identity"], returned_at=STAMP, ordinal=1),
            )
    await db_portable._export_pg_to_sqlite(test_engine, Base.metadata, restored)
    sqlite = create_async_engine(f"sqlite+aiosqlite:///{restored}")
    try:
        await assert_retained(sqlite, returned, ready, proof)
        with pytest.raises(IntegrityError):
            async with sqlite.begin() as conn:
                await conn.execute(insert(AutoQueueReturn), {**returned, "id": "another-receipt"})
    finally:
        await sqlite.dispose()
