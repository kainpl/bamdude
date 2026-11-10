"""A's scope pruning and last-login serialization on disposable native PostgreSQL."""

import pytest
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, _strip_tz_from_params, import_all_models
from backend.app.services import embedded_postgres as ep
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_scopes import (
    test_key_patch_null_empty_omitted_and_printer_delete_do_not_expand_scope as assert_prune,
)
from backend.tests.integration.test_g01_a_sso import test_concurrent_distinct_unlinks_keep_one_login as assert_unlinks

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("embedded_postgres")


@pytest.fixture
async def test_engine(live_settings, monkeypatch):
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
        event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
        import_all_models()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(
            "backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False)
        )
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        await ep.stop()


async def test_key_scope_pruning_on_postgres(async_client, db_session, printer_factory):
    await assert_prune(async_client, db_session, printer_factory)


async def test_concurrent_sso_unlink_on_postgres(async_client, db_session, monkeypatch):
    await assert_unlinks(async_client, db_session, monkeypatch)
