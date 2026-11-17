"""C01/C02/C05: the same actual HTTP account doors on isolated PostgreSQL."""

import pytest

from backend.app.core.credential_config import initialize_credential_config_anchor
from backend.tests.integration.test_content_delete_atomic import (
    test_failure_after_content_flush_rolls_back_everything_and_unlinks_nothing as assert_rollback,
    test_user_http_cleans_children_and_preserves_foreign_history as assert_children,
    test_user_http_keeps_physical_queue_owners as assert_queue_keeps,
    test_user_http_refuses_physical_keep_without_account_or_row_mutation as assert_archive_keeps,
)
from backend.tests.integration.test_content_delete_orders import (
    test_unfiling_plans_foreign_library_provenance_products_before_order_and_print_locks as assert_foreign_provenance,
)
from backend.tests.integration.test_content_delete_protocol import independent_factory  # noqa: F401
from backend.tests.integration.test_orders_api import catalog  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def test_engine(independent_factory):
    factory = independent_factory
    engine = factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL HTTP counterpart")
    async with engine.begin() as conn:
        await initialize_credential_config_anchor(conn)
    return engine


@pytest.mark.parametrize("delete_items", [True, False])
async def test_http_children(async_client, db_session, delete_items):
    await assert_children(async_client, db_session, delete_items)


@pytest.mark.parametrize("keep", ["printing", "plate_gate", "swap_owed"])
async def test_http_archive_keeps(async_client, db_session, printer_factory, keep):
    await assert_archive_keeps(async_client, db_session, printer_factory, keep)


@pytest.mark.parametrize("keep", ["current_item", "live_queue", "live_auto_execution"])
async def test_http_queue_keeps(async_client, db_session, keep):
    await assert_queue_keeps(async_client, db_session, keep)


@pytest.mark.parametrize(
    "phase", ["source_release", "usage_detach", "stock_detach", "product_detach", "photos", "account_detach"]
)
async def test_http_rollback(async_client, db_session, monkeypatch, phase):
    await assert_rollback(async_client, db_session, monkeypatch, phase)


async def test_http_foreign_provenance(committing_client, db_session, catalog, monkeypatch):
    await assert_foreign_provenance(committing_client, db_session, catalog, monkeypatch)
