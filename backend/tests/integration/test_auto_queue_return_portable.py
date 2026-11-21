"""Full backup/restore retains return facts and per-print history, never new eligibility."""

import json

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import db_portable
from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.migrations import _discover_migrations
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn, AutoQueueUnsentReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.tests.integration.test_auto_queue_return_schema import STAMP, receipt_values
from backend.tests.integration.test_migration_m253_postgres import live_settings, pg_engine  # noqa: F401
from backend.tests.unit.test_migration_m254 import assert_receipt_contract

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def seed_file(path):
    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    returned = receipt_values(returned_at=STAMP, ordinal=1)
    ready = receipt_values(auto_identity=returned["auto_identity"])
    proof = {"receipt_identity": returned["id"], "printer_identity": returned["printer_identity"]}
    refused = receipt_values(
        auto_identity=returned["auto_identity"],
        execution_archive_id=None,
        execution_archive_identity=None,
        returned_at=STAMP,
        ordinal=1,
        old_job_id=12,
    )
    unsent = {key: value for key, value in refused.items() if key in AutoQueueUnsentReturn.__table__.columns}
    unsent["failure_code"] = "dispatchPrinterUnavailable"
    unsent_proof = {"version": 1, "receipt_identity": unsent["id"], "old_job_identity": unsent["old_job_identity"]}
    try:
        async with engine.begin() as conn:
            await conn.run_sync(db_portable._portable_metadata(Base.metadata).create_all)
            await conn.execute(
                text("INSERT INTO _migrations (version, name) VALUES (:version, :name)"),
                [{"version": m["version"], "name": m["name"]} for m in _discover_migrations()],
            )
            await conn.execute(text("INSERT INTO settings (key, value) VALUES ('auto_queue_return_failed', 'true')"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add(
                Printer(
                    id=3,
                    name="old printer",
                    ip_address="127.0.0.2",
                    access_code="00000000",
                    serial_number="RETURN3",
                    model="P1S",
                    connector_identity=returned["printer_identity"],
                )
            )
            await db.flush()
            db.add(PrinterQueue(id=2, printer_id=3))
            db.add(
                PrintArchive(
                    id=8,
                    recovery_identity=returned["execution_archive_identity"],
                    printer_id=3,
                    filename="failed.3mf",
                    file_path="archive/failed.3mf",
                    file_size=7,
                    status="failed",
                    extra_data={"cost": 1.25},
                    from_auto_queue=True,
                    queue_item_identity=returned["old_job_identity"],
                    source_auto_identity=returned["auto_identity"],
                )
            )
            db.add(
                AutoQueueItem(
                    id=9,
                    recovery_identity=returned["auto_identity"],
                    target_model="P1S",
                    position=0,
                    failed_return_count=1,
                    failed_return_proof=proof,
                    unsent_return_count=1,
                    unsent_return_proof=unsent_proof,
                )
            )
            await db.flush()
            db.add(
                PrintQueueItem(
                    id=7,
                    recovery_identity=returned["old_job_identity"],
                    queue_id=2,
                    position=0,
                    status="failed",
                    error_code="physical_failure",
                    archive_id=8,
                    source_auto_identity=returned["auto_identity"],
                    returned_receipt_identity=returned["id"],
                )
            )
            db.add(
                PrintQueueItem(
                    id=12,
                    queue_id=2,
                    status="failed",
                    recovery_identity=unsent["old_job_identity"],
                    placement_basis="v1:" + "a" * 64,
                    source_auto_identity=returned["auto_identity"],
                    returned_receipt_identity=unsent["id"],
                )
            )
            await db.commit()
        async with engine.begin() as conn:
            await conn.execute(insert(AutoQueueReturn), [returned, ready])
            await conn.execute(insert(AutoQueueUnsentReturn), unsent)
    finally:
        await engine.dispose()
    return returned, ready, proof


async def assert_retained(engine, returned, ready, proof):
    async with async_sessionmaker(engine)() as db:
        auto = await db.get(AutoQueueItem, 9)
        assert auto.failed_return_count == 1 and auto.failed_return_proof == proof
        assert auto.unsent_return_count == 1
        unsent = await db.get(AutoQueueUnsentReturn, auto.unsent_return_proof["receipt_identity"])
        assert (unsent.auto_identity, unsent.ordinal, unsent.execution_archive_id, unsent.returned_at) == (
            auto.recovery_identity,
            1,
            None,
            STAMP,
        )
        retired = await db.get(PrintQueueItem, 12)
        assert retired.placement_basis == "v1:" + "a" * 64
        assert retired.returned_receipt_identity == unsent.id
        assert retired.recovery_identity == unsent.old_job_identity
        assert auto.recovery_identity == returned["auto_identity"]
        old = await db.get(PrintQueueItem, 7)
        assert (old.status, old.error_code, old.returned_receipt_identity) == (
            "failed",
            "physical_failure",
            returned["id"],
        )
        archive = await db.get(PrintArchive, 8)
        assert archive.status == "failed" and archive.extra_data == {"cost": 1.25}
        assert archive.recovery_identity == returned["execution_archive_identity"]
        assert archive.queue_item_identity == old.recovery_identity == returned["old_job_identity"]
        assert archive.source_auto_identity == auto.recovery_identity
        facts = (await db.scalars(select(AutoQueueReturn).order_by(AutoQueueReturn.id))).all()
        assert {r.id for r in facts} == {returned["id"], ready["id"]}
        done = next(r for r in facts if r.id == returned["id"])
        pending = next(r for r in facts if r.id == ready["id"])
        assert done.ordinal == 1 and done.returned_at == STAMP
        assert pending.ordinal is None and pending.returned_at is None
        assert done.source_basis == returned["source_basis"] and done.dispatch_evidence == returned["dispatch_evidence"]


async def test_full_sqlite_snapshot_retains_receipts_and_history(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "restored.db"
    returned, ready, proof = await seed_file(source)
    db_portable._snapshot_sqlite(source, target)
    engine = create_async_engine(f"sqlite+aiosqlite:///{target}")
    try:
        await assert_retained(engine, returned, ready, proof)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM auto_queue_returns")) == 2
            assert json.loads(await conn.scalar(text("SELECT failed_return_proof FROM auto_queue_items"))) == proof
    finally:
        await engine.dispose()


async def test_sqlite_import_conforms_unsent_receipts_on_postgres(pg_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_engine.url.render_as_string(hide_password=False))
    source = tmp_path / "source.db"
    returned, ready, proof = await seed_file(source)
    assert await db_portable.import_sqlite_to_postgres(pg_engine, Base.metadata, source) > 0
    await db_portable.conform_imported_schema(pg_engine, Base.metadata)
    await assert_retained(pg_engine, returned, ready, proof)
    async with pg_engine.connect() as conn:
        await conn.run_sync(assert_receipt_contract)
