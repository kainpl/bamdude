"""C32 on a disposable embedded PostgreSQL: an import keeps the numbers the sequence never handed out
and raises the sequence past them, so the next spool is numbered after the import's highest."""

import pytest

from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_inventory_csv_numbers import (
    assert_a_moved_inventory_keeps_its_numbers,
    assert_a_number_given_out_here_is_renumbered,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


async def test_a_moved_inventory_keeps_its_numbers_on_postgres(async_client, db_session):
    await assert_a_moved_inventory_keeps_its_numbers(async_client, db_session)


async def test_a_number_given_out_here_is_renumbered_on_postgres(async_client, db_session):
    await assert_a_number_given_out_here_is_renumbered(async_client, db_session)
