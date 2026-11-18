"""S2 category preflight, alias counts and cross-commit identity fences."""

from asyncio import CancelledError
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from backend.app.models.archive import PrintArchive
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services.git_restore import ARCHIVES_PATH, SPOOL_USAGE_PATH, SPOOLS_PATH, GitRestoreService
from backend.app.services.git_restore_records import PayloadShapeError
from backend.tests.integration.test_git_restore_s2 import AT, END, archive, collect, entry, printer

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"history": None},
        {"history": [], "usage_history": [{}]},
        {"history": [{"x": True}], "usage_history": [{"x": 1}]},
        {"history": [{"x": 1.0}], "usage_history": [{"x": 1}]},
    ],
)
async def test_e17_shape_refusal_before_spool_writes(db_session, payload):
    data = {
        SPOOLS_PATH: {"spools": [{"id": 1, "tag_uid": "would-write", "material": "PLA"}]},
        SPOOL_USAGE_PATH: payload,
    }
    service = GitRestoreService()
    count, detail = await service._count_items(db_session, RestoreCategory.SPOOLS, data)
    assert count == 0 and detail.code in ("spoolUsageInvalidPayload", "spoolUsageAliasConflict")
    with pytest.raises(PayloadShapeError):
        await service._apply(db_session, data, [RestoreCategory.SPOOLS], True)
    assert await db_session.scalar(select(func.count()).select_from(Spool)) == 0


@pytest.mark.parametrize("key", ["history", "usage_history", "both"])
async def test_e17_alias_count_partition_and_usage_defaults(db_session, key):
    rows = [
        {"spool_id": 1, "created_at": AT.isoformat(), "weight_used": True, "percent_used": 1.5, "status": None},
        {"spool_id": 1},
        {"spool_id": 999, "created_at": AT.isoformat()},
        False,
    ]
    usage = {"history": rows, "usage_history": rows} if key == "both" else {key: rows}
    data = {SPOOLS_PATH: {"spools": [{"id": 1, "tag_uid": "TAG", "material": "PLA"}]}, SPOOL_USAGE_PATH: usage}
    service = GitRestoreService()
    count, _ = await service._count_items(db_session, RestoreCategory.SPOOLS, data)
    result = (await service._apply(db_session, data, [RestoreCategory.SPOOLS], True))["spools"]
    assert count == result.restored + result.skipped + result.failed == 5
    assert (result.restored, result.skipped, result.failed) == (2, 2, 1)
    h = (await db_session.scalars(select(SpoolUsageHistory))).one()
    assert (h.weight_used, h.percent_used, h.status) == (0, 0, "unknown")


async def test_e18_deleted_reused_archive_id_drops_link_after_category_commit(db_session):
    a = await archive(db_session)
    original_id = a.id
    s = Spool(material="PLA", tag_uid="TAG")
    db_session.add(s)
    await db_session.commit()
    db_session.add(SpoolUsageHistory(spool_id=s.id, archive_id=a.id, weight_used=1, created_at=AT))
    await db_session.commit()
    data = await collect(db_session)
    await db_session.execute(delete(SpoolUsageHistory))
    await db_session.commit()

    class ReusingRestore(GitRestoreService):
        async def _restore_spools(self, db, *args, **kwargs):
            await db.execute(delete(PrintArchive).where(PrintArchive.id == original_id))
            db.add(PrintArchive(id=original_id, filename="NEW", file_path="", file_size=0, status="completed"))
            await db.commit()
            await self._begin_record_category(db)
            return await super()._restore_spools(db, *args, **kwargs)

    result = await ReusingRestore()._apply(db_session, data, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], False)
    h = (await db_session.scalars(select(SpoolUsageHistory))).one()
    assert h.archive_id is None
    assert any(n["code"] == "spoolUsageUnlinked" for n in result["spools"].notes)


async def test_e19_previous_committed_category_survives_cancellation(db_session):
    class FailingRestore(GitRestoreService):
        async def _restore_spools(self, *args, **kwargs):
            raise CancelledError()

    results = {}
    with pytest.raises(CancelledError):
        await FailingRestore()._apply(
            db_session,
            {ARCHIVES_PATH: {"archives": [entry(recovery_identity=str(uuid4()))]}, SPOOLS_PATH: {"spools": []}},
            [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS],
            True,
            results=results,
        )
    await db_session.rollback()
    assert list(results) == ["archives"] and results["archives"].restored == 1
    assert await db_session.scalar(select(func.count()).select_from(PrintArchive)) == 1


async def test_e18_names_never_choose_the_archive_and_conflicting_printer_stays_unlinked(db_session):
    p, q = await printer(db_session, "ONE"), await printer(db_session, "TWO")
    a = await archive(db_session, printer_id=p.id, print_name="same")
    b = await archive(db_session, printer_id=q.id, filename="second", print_name="same")
    s = Spool(material="PLA", tag_uid="TAG")
    db_session.add(s)
    await db_session.commit()
    db_session.add_all(
        [
            SpoolUsageHistory(
                spool_id=s.id,
                archive_id=a.id,
                printer_id=q.id,
                print_name="same",
                weight_used=1,
                created_at=AT,
                status="runout",
                cost=0,
            ),
            SpoolUsageHistory(
                spool_id=s.id,
                archive_id=b.id,
                printer_id=q.id,
                print_name="same",
                weight_used=2,
                created_at=END,
                status="ams_sync",
                cost=3,
            ),
        ]
    )
    await db_session.commit()
    files = await collect(db_session)
    await db_session.execute(delete(SpoolUsageHistory))
    await db_session.commit()
    await GitRestoreService()._apply(db_session, files, [RestoreCategory.ARCHIVES, RestoreCategory.SPOOLS], False)
    rows = (await db_session.scalars(select(SpoolUsageHistory).order_by(SpoolUsageHistory.created_at))).all()
    assert [(r.archive_id, r.printer_id, r.status, r.cost) for r in rows] == [
        (a.id, None, "runout", 0),
        (b.id, q.id, "ams_sync", 3),
    ]
    # A spool-only restore has no committed accepted archive proof. Existing
    # rows keep their original links, and are not retrofitted by their names.
    await GitRestoreService()._apply(db_session, files, [RestoreCategory.SPOOLS], False)
    assert len((await db_session.scalars(select(SpoolUsageHistory))).all()) == 2
    await db_session.execute(delete(SpoolUsageHistory))
    await db_session.commit()
    await GitRestoreService()._apply(db_session, files, [RestoreCategory.SPOOLS], False)
    rows = (await db_session.scalars(select(SpoolUsageHistory))).all()
    assert len(rows) == 2 and all(row.archive_id is None for row in rows)
