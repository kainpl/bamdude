"""Optional disposable external-PG gate for hot queue and picker reads.

Run only with HOT_API_TEST_DATABASE_URL set to a dedicated empty database.
The fixture creates and drops schema, so never point it at an application DB.
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.api.routes.inventory import spool_picker
from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.models.spool import Spool
from backend.tests.integration.test_hot_queue_reads import (
    test_virtual_auxiliary_selects_are_batch_bounded as assert_batch_sql,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
async def test_engine(monkeypatch):
    url = os.environ.get("HOT_API_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable HOT_API_TEST_DATABASE_URL")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("HOT_API_TEST_DATABASE_URL must use PostgreSQL asyncpg")
    # The shared suite pins the dialect helper to its SQLite engine. These
    # tests must exercise the PostgreSQL branch against the PostgreSQL engine.
    monkeypatch.setattr(settings, "database_url", url)
    schema = f"hot_api_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = None
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        import_all_models()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        # Isolated per-test schema; dropping it avoids the app's cyclic FKs.
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_virtual_batch_sql_on_external_postgres(db_session, printer_factory, archive_factory, monkeypatch):
    await assert_batch_sql(db_session, printer_factory, archive_factory, monkeypatch)


async def test_picker_profile_qualifier_on_external_postgres(db_session):
    spool = Spool(
        material="ABS",
        brand="Test",
        color_name="Blue",
        slicer_filament_name="Test_1   @H2D",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()
    result = await spool_picker(
        printer_id=1,
        ams_id=0,
        tray_id=0,
        tray_profile="Test_1",
        tray_material="PETG",
        q="",
        show_all=False,
        replacing_spool_id=None,
        page=1,
        per_page=50,
        db=db_session,
        _=None,
    )
    assert [item.id for item in result.items] == [spool.id]
