"""Real two-session restore races on file WAL and an opt-in disposable PG DB.

Set G02_S2_TEST_PG_URL to a dedicated empty test database. Never a farm DB.
Run this module without xdist when exercising that one shared database.
"""

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, import_all_models
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services import git_restore_identity as identity
from backend.app.services.archive import ArchiveService
from backend.app.services.git_restore import ARCHIVES_PATH, SPOOL_USAGE_PATH, SPOOLS_PATH, GitRestoreService
from backend.tests.integration.test_git_restore_s2 import AT, END, archive, collect, entry, printer, restore

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=["wal", "pg"])
async def race_db(request, tmp_path):
    import_all_models()
    pg = request.param == "pg"
    url = (
        os.environ.get("G02_S2_TEST_PG_URL") if pg else "sqlite+aiosqlite:///" + (tmp_path / "owned.sqlite").as_posix()
    )
    if not url:
        pytest.skip("dedicated G02_S2_TEST_PG_URL not configured")
    admin = create_async_engine(url) if pg else None
    schema = "g02_s2_" + uuid4().hex
    if admin:
        async with admin.begin() as db:
            await db.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}} if pg else {"timeout": 15}
    )
    if not pg:

        @event.listens_for(engine.sync_engine, "connect")
        def setup(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")

    async with engine.begin() as db:
        if not pg:
            assert await db.scalar(text("PRAGMA journal_mode=WAL")) == "wal"
        await db.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), pg
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as db:
                await db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            await admin.dispose()


async def test_e20_completion_between_plan_and_lock_preserves_attached_file(race_db, monkeypatch):
    factory, pg = race_db
    async with factory() as db:
        p = await printer(db)
        a = await archive(db, printer_id=p.id, status="printing", quantity=3)
        key, uid = a.id, a.recovery_identity
    payload = entry(recovery_identity=uid, status="printing", filename="old.gcode", quantity=0, notes="operator")
    if pg:
        original = identity.lock_prints

        async def finish_before_lock(db, ids):
            async with factory() as other:
                await ArchiveService(other).update_archive_status(
                    key, "completed", completed_at=END, expected_identity=uid
                )
                await other.execute(
                    update(PrintArchive)
                    .where(PrintArchive.id == key)
                    .values(filename="attached.3mf", file_path="/owned", total_layers=693)
                )
                await other.commit()
            return await original(db, ids)

        monkeypatch.setattr(identity, "lock_prints", finish_before_lock)
        async with factory() as db:
            await restore(db, [payload])
    else:
        # SQLite must reserve its writer before the first identity read.
        entered = asyncio.Event()

        class AnnouncedRestore(GitRestoreService):
            @staticmethod
            async def _begin_record_category(db):
                entered.set()
                await GitRestoreService._begin_record_category(db)

        async with factory() as finishing:
            await finishing.execute(text("BEGIN IMMEDIATE"))
            await finishing.execute(
                update(PrintArchive)
                .where(PrintArchive.id == key)
                .values(status="completed", filename="attached.3mf", file_path="/owned", total_layers=693)
            )

            async def waiting_restore():
                async with factory() as db:
                    await restore(db, [payload], service=AnnouncedRestore())

            task = asyncio.create_task(waiting_restore())
            await asyncio.wait_for(entered.wait(), 5)
            assert not task.done()
            await finishing.commit()
            await asyncio.wait_for(task, 10)
    async with factory() as db:
        current = await db.get(PrintArchive, key)
        assert (
            current.status,
            current.filename,
            current.file_path,
            current.total_layers,
            current.quantity,
            current.notes,
        ) == ("completed", "attached.3mf", "/owned", 693, 3, "operator")


async def test_e20_printer_serial_changes_before_lock_do_not_authorize_old_id(race_db, monkeypatch):
    factory, pg = race_db
    async with factory() as db:
        p = await printer(db)
        key = p.id
    original = identity.lock_printers

    async def replace_before_lock(db, ids):
        if pg:
            async with factory() as other:
                await other.execute(update(Printer).where(Printer.id == key).values(serial_number="NEW"))
                await other.commit()
        return await original(db, ids)

    if pg:
        monkeypatch.setattr(identity, "lock_printers", replace_before_lock)
    else:
        async with factory() as db:
            await db.execute(update(Printer).where(Printer.id == key).values(serial_number="NEW"))
            await db.commit()
    async with factory() as db:
        await restore(db, [entry(printer_id=key, printer_serial="P-A")])
        assert (await db.scalars(select(PrintArchive))).one().printer_id is None


async def test_e20_unplanned_uuid_candidate_refuses_category(race_db, monkeypatch):
    factory, pg = race_db
    if not pg:
        pytest.skip("WAL writer reservation prevents insertion after planning")
    from uuid import uuid4

    uid = str(uuid4())
    original = identity.lock_printers

    async def insert_before_locks(db, ids):
        async with factory() as other:
            await archive(other, recovery_identity=uid)
        return await original(db, ids)

    monkeypatch.setattr(identity, "lock_printers", insert_before_locks)
    async with factory() as db:
        with pytest.raises(RuntimeError, match="identity changed"):
            await restore(db, [entry(recovery_identity=uid)])
        await db.rollback()
    async with factory() as db:
        assert len((await db.scalars(select(PrintArchive))).all()) == 1


async def test_e20_spool_lifetime_is_held_through_usage_flush(race_db):
    factory, pg = race_db
    async with factory() as db:
        s = Spool(material="PLA", tag_uid="TAG")
        db.add(s)
        await db.commit()
        sid = s.id
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedUsage(GitRestoreService):
        async def _restore_spool_usage(self, *args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            return await super()._restore_spool_usage(*args, **kwargs)

    async def importing():
        async with factory() as db:
            await PausedUsage()._apply(
                db,
                {
                    SPOOLS_PATH: {"spools": [{"id": 1, "material": "PLA", "tag_uid": "TAG"}]},
                    SPOOL_USAGE_PATH: {"history": [{"spool_id": 1, "weight_used": 5, "created_at": AT.isoformat()}]},
                },
                [RestoreCategory.SPOOLS],
                False,
            )

    task = asyncio.create_task(importing())
    await asyncio.wait_for(entered.wait(), 10)
    async with factory() as other:
        # A NOWAIT delete on PG proves a genuine row lock; SQLite's writer
        # ownership already prevents all concurrent deletes in this category.
        if pg:
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                await other.execute(select(Spool).where(Spool.id == sid).with_for_update(nowait=True))
            await other.rollback()
        else:
            await other.execute(text("PRAGMA busy_timeout=1"))
            from sqlalchemy.exc import OperationalError

            with pytest.raises(OperationalError, match="locked"):
                await other.execute(delete(Spool).where(Spool.id == sid))
            await other.rollback()
    release.set()
    await asyncio.wait_for(task, 10)
    async with factory() as db:
        h = (await db.scalars(select(SpoolUsageHistory))).one()
        assert h.spool_id == sid and h.weight_used == 5


async def test_e20_uuid_insert_race_is_a_category_failure(race_db):
    factory, pg = race_db
    if not pg:
        pytest.skip("WAL writer excludes the competing insert")
    uid = str(uuid4())

    class RacingRestore(GitRestoreService):
        async def _find_archive(self, db, *args, **kwargs):
            found = await super()._find_archive(db, *args, **kwargs)
            assert found is None
            async with factory() as other:
                await archive(other, recovery_identity=uid)
            return found

    results = {}
    async with factory() as db:
        with pytest.raises(IntegrityError):
            await RacingRestore()._apply(
                db,
                {ARCHIVES_PATH: {"archives": [entry(recovery_identity=uid)]}},
                [RestoreCategory.ARCHIVES],
                True,
                results=results,
            )
        await db.rollback()
    assert results == {}
    async with factory() as db:
        assert len((await db.scalars(select(PrintArchive))).all()) == 1


async def test_e20_reverse_batches_keep_sorted_locks_and_link_usage(race_db):
    factory, pg = race_db
    async with factory() as db:
        p = await printer(db)
        a = await archive(db, printer_id=p.id, energy_kwh=0, energy_cost=0)
        b = await archive(db, filename="second", printer_id=p.id)
        s = Spool(material="PLA", tag_uid="TAG")
        db.add(s)
        await db.commit()
        db.add(SpoolUsageHistory(spool_id=s.id, archive_id=a.id, printer_id=p.id, weight_used=3, created_at=AT))
        await db.commit()
        files = await collect(db)
        await db.execute(delete(SpoolUsageHistory))
        await db.commit()
        expected = (a.id, b.id)
    reversed_files = {
        **files,
        ARCHIVES_PATH: {**files[ARCHIVES_PATH], "archives": list(reversed(files[ARCHIVES_PATH]["archives"]))},
    }

    async def applying(payload):
        async with factory() as db:
            return await GitRestoreService()._apply(
                db, payload, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], True
            )

    await asyncio.wait_for(asyncio.gather(applying(files), applying(reversed_files)), 20)
    async with factory() as db:
        rows = (await db.scalars(select(PrintArchive).order_by(PrintArchive.id))).all()
        assert tuple(r.id for r in rows) == expected and rows[0].energy_kwh == 0
        h = (await db.scalars(select(SpoolUsageHistory))).one()
        assert h.archive_id == expected[0] and h.printer_id == p.id
