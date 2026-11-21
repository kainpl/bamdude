"""m254 upgrade and fresh schema parity on disposable PostgreSQL (G03 B2)."""

import pytest

from backend.app.core.database import Base, import_all_models
from backend.app.migrations import m254_auto_queue_unsent_returns as migration
from backend.tests.integration.test_migration_m253_postgres import live_settings, pg_engine  # noqa: F401
from backend.tests.unit.test_migration_m254 import assert_receipt_contract, assert_upgrade_preserves_legacy_rows

pytestmark = [pytest.mark.integration, pytest.mark.slow]


async def test_unsent_receipts_upgrade_and_replay_on_postgres(pg_engine):
    await assert_upgrade_preserves_legacy_rows(pg_engine)


async def test_fresh_postgres_and_the_migration_agree_on_unsent_receipts(pg_engine):
    import_all_models()
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await migration.upgrade(conn)
        await conn.run_sync(assert_receipt_contract)
