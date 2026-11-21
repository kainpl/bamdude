import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m254_auto_queue_unsent_returns as migration
from backend.app.models.auto_queue_return import AutoQueueUnsentReturn


@pytest.mark.asyncio
async def test_unsent_receipts_upgrade_and_replay_preserve_legacy_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm254.db'}")
    try:
        await assert_upgrade_preserves_legacy_rows(engine)
    finally:
        await engine.dispose()


def assert_receipt_contract(sync):
    table = AutoQueueUnsentReturn.__table__
    schema = inspect(sync)
    assert schema.get_foreign_keys(table.name) == []
    assert {c["name"]: c["nullable"] for c in schema.get_columns(table.name)} == {
        c.name: c.nullable for c in table.columns
    }
    assert {tuple(c["column_names"]) for c in schema.get_unique_constraints(table.name)} == {
        ("old_job_identity",),
        ("auto_identity", "ordinal"),
    }
    assert {c["name"] for c in schema.get_check_constraints(table.name)} == {"ck_aq_unsent_return_ordinal"}


async def assert_upgrade_preserves_legacy_rows(engine):
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE print_queue (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO print_queue VALUES (1)")
        await conn.exec_driver_sql("INSERT INTO auto_queue_items VALUES (2)")
        await migration.upgrade(conn)
        assert (await conn.scalar(text("SELECT placement_basis FROM print_queue"))) is None
        assert (
            await conn.execute(text("SELECT unsent_return_count, unsent_return_proof FROM auto_queue_items"))
        ).one() == (0, None)
        await conn.exec_driver_sql("UPDATE auto_queue_items SET unsent_return_count = 1 WHERE id = 2")
        await migration.upgrade(conn)
        assert (await conn.scalar(text("SELECT unsent_return_count FROM auto_queue_items"))) == 1
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.exec_driver_sql("UPDATE auto_queue_items SET unsent_return_count = -1 WHERE id = 2")
        await conn.run_sync(assert_receipt_contract)
        assert (await conn.scalar(text("SELECT count(*) FROM auto_queue_unsent_returns"))) == 0
