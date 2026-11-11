"""m217 on a disposable PostgreSQL: per-row AutoQueue identities, unique index, empty lineage, idempotent re-run."""

import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.migrations import helpers, m217_job_lineage as m217
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


async def test_an_existing_postgres_database_gets_identities_and_lineage_columns(test_engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.models.auto_queue import AutoQueueItem

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:  # real rows first, through the model, so every NOT NULL column has its value
        db.add_all([AutoQueueItem(target_model="X1C", position=n) for n in (1, 2, 3)])
        await db.commit()
    async with test_engine.begin() as conn:  # ... then the database as it stood before m217
        await conn.execute(text("DROP INDEX IF EXISTS ix_auto_queue_items_recovery_identity"))
        await conn.execute(text("ALTER TABLE auto_queue_items DROP COLUMN IF EXISTS recovery_identity"))
        await conn.execute(text("DROP INDEX IF EXISTS ix_print_queue_source_auto_identity"))
        await conn.execute(text("ALTER TABLE print_queue DROP COLUMN IF EXISTS source_auto_identity"))
        for column in ("queue_item_identity", "source_auto_identity"):
            await conn.execute(text(f"DROP INDEX IF EXISTS ix_print_archives_{column}"))
            await conn.execute(text(f"ALTER TABLE print_archives DROP COLUMN IF EXISTS {column}"))

    for _ in range(2):
        async with test_engine.begin() as conn:
            await m217.upgrade(conn)

    async with test_engine.begin() as conn:
        values = [r[0] for r in (await conn.execute(text("SELECT recovery_identity FROM auto_queue_items"))).all()]
        assert len(values) == 3 and all(str(uuid.UUID(v)) == v for v in values)
        assert len(set(values)) == 3
        indexes = await conn.run_sync(
            lambda sync: {i["name"]: i for i in inspect(sync).get_indexes("auto_queue_items")}
        )
        assert indexes["ix_auto_queue_items_recovery_identity"]["unique"]
        queue_columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("print_queue")})
        archive_columns = await conn.run_sync(
            lambda sync: {c["name"] for c in inspect(sync).get_columns("print_archives")}
        )
        assert "source_auto_identity" in queue_columns
        assert {"queue_item_identity", "source_auto_identity"} <= archive_columns
