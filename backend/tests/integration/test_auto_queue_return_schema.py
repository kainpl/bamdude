"""Failed-return foundation on real disposable files; no return worker is registered."""

import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, import_all_models
from backend.app.migrations import m226_auto_queue_return_facts as migration
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.print_queue import PrintQueueItem

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
STAMP = datetime(2026, 11, 14, 12)


def receipt_values(**changes):
    values = {
        "id": str(uuid.uuid4()),
        "old_job_id": 7,
        "old_job_identity": str(uuid.uuid4()),
        "old_queue_id": 2,
        "execution_archive_id": 8,
        "execution_archive_identity": str(uuid.uuid4()),
        "auto_item_id": 9,
        "auto_identity": str(uuid.uuid4()),
        "printer_id": 3,
        "printer_identity": str(uuid.uuid4()),
        "owner_identity": str(uuid.uuid4()),
        "source_basis": {"version": 1, "sha256": "a" * 64, "policy": {"force_color_match": True}},
        "source_basis_digest": "b" * 64,
        "dispatch_evidence": {"started_at": STAMP.isoformat(), "dispatch_attempts": 1},
        "completed_at": STAMP,
        "ready_at": STAMP,
        "returned_at": None,
        "ordinal": None,
    }
    return {**values, **changes}


async def legacy_schema(conn):
    primary_key = "SERIAL PRIMARY KEY" if conn.dialect.name == "postgresql" else "INTEGER PRIMARY KEY"
    for ddl in (
        "CREATE TABLE auto_queue_items (id INTEGER PRIMARY KEY, recovery_identity VARCHAR(36), status VARCHAR(20))",
        "CREATE TABLE print_queue (id INTEGER PRIMARY KEY, recovery_identity VARCHAR(36), status VARCHAR(20), "
        "error_code VARCHAR(64), archive_id INTEGER)",
        f"CREATE TABLE settings (id {primary_key}, key VARCHAR(100) UNIQUE, value TEXT)",
    ):
        await conn.execute(text(ddl))
    await conn.execute(text("INSERT INTO auto_queue_items VALUES (9, 'auto-old', 'assigned')"))
    await conn.execute(text("INSERT INTO print_queue VALUES (7, 'job-old', 'failed', 'physical_failure', 8)"))


@pytest.fixture
async def foundation_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'foundation.db'}")
    import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_fresh_defaults_and_migration_do_not_manufacture_settlement(foundation_engine):
    async with async_sessionmaker(foundation_engine, expire_on_commit=False)() as db:
        auto = AutoQueueItem(target_model="P1S", position=0, status="assigned")
        old = PrintQueueItem(queue_id=2, position=0, status="failed", error_code="physical")
        db.add_all([auto, old])
        await db.commit()
        assert auto.failed_return_count == 0 and auto.failed_return_proof is None
        assert old.returned_receipt_identity is None and old.error_code == "physical"
    async with foundation_engine.begin() as conn:
        for _ in range(2):
            await migration.upgrade(conn)
        assert await conn.scalar(text("SELECT count(*) FROM auto_queue_returns")) == 0
        assert await conn.scalar(text("SELECT value FROM settings WHERE key='auto_queue_return_failed'")) == "false"


async def test_upgrade_keeps_legacy_failure_and_never_infers_ready(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await legacy_schema(conn)
            for _ in range(2):
                await migration.upgrade(conn)
            assert tuple((await conn.execute(text("SELECT * FROM auto_queue_items"))).one()) == (
                9,
                "auto-old",
                "assigned",
                0,
                None,
            )
            assert tuple((await conn.execute(text("SELECT * FROM print_queue"))).one()) == (
                7,
                "job-old",
                "failed",
                "physical_failure",
                8,
                None,
            )
            assert await conn.scalar(text("SELECT count(*) FROM auto_queue_returns")) == 0
            assert await conn.scalar(text("SELECT value FROM settings WHERE key='auto_queue_return_failed'")) == "false"
            await conn.execute(text("UPDATE settings SET value='true' WHERE key='auto_queue_return_failed'"))
            await conn.execute(text("UPDATE auto_queue_items SET failed_return_count=2, failed_return_proof='{}'"))
            await conn.execute(text("UPDATE print_queue SET returned_receipt_identity='retained-receipt'"))
            await conn.execute(text("DROP INDEX ix_aq_return_ready_time"))
            await migration.upgrade(conn)
            assert await conn.scalar(text("SELECT failed_return_count FROM auto_queue_items")) == 2
            assert await conn.scalar(text("SELECT failed_return_proof FROM auto_queue_items")) == "{}"
            assert await conn.scalar(text("SELECT returned_receipt_identity FROM print_queue")) == "retained-receipt"
            assert await conn.scalar(text("SELECT value FROM settings WHERE key='auto_queue_return_failed'")) == "true"
            assert await conn.run_sync(lambda c: inspect(c).get_indexes("auto_queue_returns"))
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE auto_queue_items SET failed_return_count=-1"))
            await conn.execute(text("INSERT INTO auto_queue_items (id, status) VALUES (10, 'pending')"))
            assert await conn.scalar(text("SELECT failed_return_count FROM auto_queue_items WHERE id=10")) == 0
    finally:
        await engine.dispose()


async def assert_uniqueness(engine):
    table = AutoQueueReturn.__table__
    first = receipt_values()
    second = receipt_values(auto_identity=first["auto_identity"])
    async with engine.begin() as conn:
        # NULL ordinal is deliberately distinct: ready facts do not consume a slot.
        await conn.execute(insert(table), [first, second])
    for conflict in (
        receipt_values(
            old_job_identity=first["old_job_identity"], execution_archive_identity=first["execution_archive_identity"]
        ),
        receipt_values(auto_identity=first["auto_identity"], returned_at=STAMP, ordinal=1),
    ):
        if conflict["ordinal"]:
            async with engine.begin() as conn:
                await conn.execute(table.update().where(table.c.id == first["id"]).values(returned_at=STAMP, ordinal=1))
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(insert(table), conflict)
    async with engine.connect() as conn:
        rows = (await conn.execute(select(table.c.id, table.c.ordinal).order_by(table.c.id))).all()
        assert dict(rows) == {first["id"]: 1, second["id"]: None}


async def test_unique_attempt_and_return_ordinal_are_atomic(foundation_engine):
    await assert_uniqueness(foundation_engine)


async def test_frozen_migration_and_fresh_model_have_same_receipt_contract(foundation_engine, tmp_path):
    legacy = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema-parity.db'}")

    def contract(conn):
        inspector = inspect(conn)
        table = "auto_queue_returns"
        return {
            "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)],
            "pk": inspector.get_pk_constraint(table)["constrained_columns"],
            "unique": sorted((u["name"], u["column_names"]) for u in inspector.get_unique_constraints(table)),
            "check": inspector.get_check_constraints(table),
            "indexes": inspector.get_indexes(table),
            "fks": inspector.get_foreign_keys(table),
        }

    try:
        async with legacy.begin() as conn:
            await legacy_schema(conn)
            await migration.upgrade(conn)
            upgraded = await conn.run_sync(contract)
        async with foundation_engine.connect() as conn:
            assert upgraded == await conn.run_sync(contract)
    finally:
        await legacy.dispose()


@pytest.mark.parametrize("returned,ordinal", [(None, 1), (STAMP, None), (STAMP, 0), (STAMP, -1)])
async def test_half_publication_and_nonpositive_ordinal_refused(foundation_engine, returned, ordinal):
    with pytest.raises(IntegrityError):
        async with foundation_engine.begin() as conn:
            await conn.execute(insert(AutoQueueReturn), receipt_values(returned_at=returned, ordinal=ordinal))


async def test_negative_return_count_refused(foundation_engine):
    async with async_sessionmaker(foundation_engine)() as db:
        db.add(AutoQueueItem(target_model="P1S", position=0, failed_return_count=-1))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_receipt_survives_delete_and_numeric_id_reuse(foundation_engine):
    record = receipt_values(old_job_id=7, auto_item_id=9, returned_at=STAMP, ordinal=1)
    async with foundation_engine.begin() as conn:
        await conn.execute(insert(AutoQueueReturn), record)
        # No reference can turn this historical identity into a newly issued job.
        assert await conn.run_sync(lambda c: inspect(c).get_foreign_keys("auto_queue_returns")) == []
    async with async_sessionmaker(foundation_engine, expire_on_commit=False)() as db:
        old = AutoQueueItem(id=9, target_model="P1S", position=0, recovery_identity=record["auto_identity"])
        db.add(old)
        await db.commit()
        await db.delete(old)
        await db.commit()
        new = AutoQueueItem(id=9, target_model="P1S", position=0)
        db.add(new)
        await db.commit()
        assert new.recovery_identity != record["auto_identity"]
        assert new.failed_return_count == 0 and new.failed_return_proof is None
        retained = await db.get(AutoQueueReturn, record["id"])
        assert retained.auto_identity == record["auto_identity"] and retained.ordinal == 1


async def assert_unique_race(engine, key):
    first = receipt_values(returned_at=STAMP, ordinal=1)
    second = receipt_values(
        auto_identity=first["auto_identity"],
        returned_at=STAMP,
        ordinal=1,
    )
    if key == "attempt":
        second = receipt_values(
            old_job_identity=first["old_job_identity"],
            execution_archive_identity=first["execution_archive_identity"],
        )
    start = asyncio.Event()

    async def publish(values):
        await start.wait()
        try:
            async with engine.begin() as conn:
                await conn.execute(insert(AutoQueueReturn), values)
            return "committed"
        except IntegrityError:
            return "duplicate"

    tasks = [asyncio.create_task(publish(row)) for row in (first, second)]
    start.set()
    assert sorted(await asyncio.gather(*tasks)) == ["committed", "duplicate"]
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM auto_queue_returns")) == 1


@pytest.mark.parametrize("key", ["attempt", "ordinal"])
async def test_unique_conflicts_across_file_sqlite_connections(foundation_engine, key):
    await assert_unique_race(foundation_engine, key)
