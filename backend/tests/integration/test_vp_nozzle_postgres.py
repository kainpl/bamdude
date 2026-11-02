"""GH106 database contracts, on an explicitly owned disposable PostgreSQL."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, _strip_tz_from_params, import_all_models
from backend.tests.integration.test_vp_nozzle import (  # noqa: F401
    test_delete_target_cannot_rebind_vp_to_reused_printer_id,
    test_http_configuration_round_trip_and_null_rejection,
    test_migration_backfills_existing_rows_and_is_repeatable,
    test_queue_selection_skips_only_positive_diameter_mismatch,
    test_rejected_delete_rolls_back_binding,
    test_sync_hot_updates_both_fields_without_dropping_sessions,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("VP_NOZZLE_TEST_POSTGRES_URL"), reason="requires disposable PostgreSQL"
)


@pytest.fixture
async def test_engine(monkeypatch):
    url = os.environ["VP_NOZZLE_TEST_POSTGRES_URL"]
    # Migration helpers inspect public, just like production. Give each test
    # an entire disposable database rather than a search_path-only schema.
    database = "vp_nozzle_" + uuid4().hex
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{database}"'))
    test_url = make_url(url).set(database=database)
    monkeypatch.setattr(settings, "database_url", test_url.render_as_string(hide_password=False))
    engine = create_async_engine(test_url, connect_args={"server_settings": {"timezone": "UTC"}})
    event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    import_all_models()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(
            "backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False)
        )
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()
