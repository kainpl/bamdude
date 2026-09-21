"""Stagger's one-time rewrite on disposable bundled PostgreSQL, not the operator DB."""

import json

import pytest
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import settings
from backend.app.migrations import m181_stagger_group_overrides as migration
from backend.app.models.settings import Settings
from backend.app.services import embedded_postgres as ep
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("embedded_postgres")


async def test_override_migration_on_postgres(live_settings):
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
            await conn.run_sync(Settings.__table__.create)
            await conn.execute(
                text("INSERT INTO settings (key, value) VALUES (:key, :value)"),
                [
                    {"key": "stagger_concurrent", "value": "2"},
                    {"key": "stagger_tag_limits", "value": '{"1":6,"2":1}'},
                    {"key": "stagger_location_limits", "value": '{"10":4}'},
                ],
            )
            await migration.upgrade(conn)
        async with engine.begin() as conn:
            saved = dict((await conn.execute(text("SELECT key, value FROM settings"))).all())
            assert json.loads(saved["stagger_tag_limits"]) == {"1": 2, "2": 1}
            assert json.loads(saved["stagger_location_limits"]) == {"10": 2}
            await conn.execute(
                text("UPDATE settings SET value = :value WHERE key = :key"),
                {"key": "stagger_tag_limits", "value": '{"1":6}'},
            )
        async with engine.begin() as conn:
            await migration.upgrade(conn)
            value = await conn.scalar(text("SELECT value FROM settings WHERE key = 'stagger_tag_limits'"))
            assert value == '{"1":6}'
    finally:
        if engine is not None:
            await engine.dispose()
        await ep.stop()
