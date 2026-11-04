"""m216 on a disposable PostgreSQL: per-row backfill, unique index, NOT NULL after the backfill, idempotent re-run."""

import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.migrations import helpers, m216_connector_identity as m216
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


async def test_an_existing_postgres_database_gets_identities_and_not_null(test_engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.models.api_key import APIKey
    from backend.app.models.printer import Printer

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:  # real rows first, through the model, so every NOT NULL column has its value
        db.add_all(
            [
                Printer(name=f"P{n}", serial_number=f"S{n}", ip_address=f"192.0.2.{n}", access_code="x")
                for n in (1, 2, 3)
            ]
            + [APIKey(name=f"K{n}", key_hash=f"h{n}", key_prefix=f"bd_{n}") for n in (1, 2)]
        )
        await db.commit()
    async with test_engine.begin() as conn:  # ... then the database as it stood before m216
        await conn.execute(text("DROP TABLE IF EXISTS connector_submission_entries"))
        await conn.execute(text("DROP TABLE IF EXISTS connector_submissions"))
        for table in m216.IDENTITY_TABLES:
            await conn.execute(text(f"DROP INDEX IF EXISTS ix_{table}_connector_identity"))
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS connector_identity"))

    for _ in range(2):
        async with test_engine.begin() as conn:
            await m216.upgrade(conn)

    async with test_engine.begin() as conn:
        for table in m216.IDENTITY_TABLES:
            values = [r[0] for r in (await conn.execute(text(f"SELECT connector_identity FROM {table}"))).all()]
            assert values and all(str(uuid.UUID(v)) == v for v in values)
            assert len(set(values)) == len(values)
            column = await conn.run_sync(
                lambda sync, t=table: next(c for c in inspect(sync).get_columns(t) if c["name"] == "connector_identity")
            )
            assert column["nullable"] is False
            indexes = await conn.run_sync(lambda sync, t=table: {i["name"]: i for i in inspect(sync).get_indexes(t)})
            assert indexes[f"ix_{table}_connector_identity"]["unique"]
        names = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert {"connector_submissions", "connector_submission_entries"} <= names
        seeded = dict(
            (
                await conn.execute(
                    text("SELECT key, value FROM settings WHERE key IN (:a, :b)"),
                    {"a": m216.SERVER_ID_KEY, "b": m216.STORAGE_EPOCH_KEY},
                )
            ).all()
        )
        assert set(seeded) == {m216.SERVER_ID_KEY, m216.STORAGE_EPOCH_KEY}
