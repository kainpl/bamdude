"""AutoQueue's final claim re-reads the swap basis it decided on (swap-source-declaration spec r3 §6; plan T9.4).

``_assign`` reads the router row's declaration, decides our swap macros from it and builds the promoted snapshot,
all before its claim. A refresh (or a source declaration a pre-fix snapshot falls back to) committed in between must
not reach the printer queue under the old decision: under the claim the row, its blob and the EFFECTIVE fact are read
again -- fresh, not out of the session's identity map -- and any difference aborts this assignment with no child.
The next pass decides everything from the new state.

A change is made here the way another session's commit looks to this one: a statement that writes the rows behind
the session's back, so the ORM objects ``_assign`` already holds keep the old values while a fresh read sees the
new ones. Two real sessions over PostgreSQL are the acceptance scenarios (plan T9.6, 7).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import auto_queue_scheduler as scheduler_module, swap_compatibility as sc
from backend.app.services.auto_queue_scheduler import AutoPlacementConflict, AutoQueueScheduler
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def an_auto_job(committing_client, db_session, source, *, fact="unknown") -> AutoQueueItem:
    response = await committing_client.post("/api/v1/auto-queue/", json={"library_file_id": source.id})
    assert response.status_code == 200, response.text
    item = await db_session.get(AutoQueueItem, response.json()["id"])
    snapshot = dict(item.source_snapshot)
    if fact == "missing":
        snapshot.pop(sc.SNAPSHOT_FACT_KEY, None)
        snapshot.pop(sc.SNAPSHOT_REASON_KEY, None)
    else:
        snapshot = sc.snapshot_with_fact(snapshot, sc.UNKNOWN_SOURCE)
    item.source_snapshot = snapshot
    await db_session.commit()
    return item


async def refreshed_behind_the_session(db, item_id: int, value: bool) -> None:
    current = (await db.execute(select(AutoQueueItem.source_snapshot).where(AutoQueueItem.id == item_id))).scalar_one()
    await db.execute(  # the table, not the mapped class: an ORM update would bring the session's objects along
        update(AutoQueueItem.__table__)
        .where(AutoQueueItem.__table__.c.id == item_id)
        .values(source_snapshot=sc.snapshot_with_fact(current, sc.SourceFact(value)))
    )


async def children(db, item_id: int) -> list[PrintQueueItem]:
    return (
        (await db.execute(select(PrintQueueItem).where(PrintQueueItem.source_auto_item_id == item_id))).scalars().all()
    )


async def test_a_refresh_before_the_first_read_is_simply_what_the_assignment_reads(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    item = await an_auto_job(committing_client, db_session, source)
    item_id, printer_id = item.id, printer.id
    await refreshed_behind_the_session(db_session, item_id, True)
    await db_session.commit()
    db_session.expire_all()
    item, printer = await db_session.get(AutoQueueItem, item_id), await db_session.get(Printer, printer_id)

    queued = await AutoQueueScheduler()._assign(db_session, item, printer)

    assert sc.snapshot_fact(queued.source_snapshot) == sc.SourceFact(True)


@pytest.mark.parametrize("window", ["after_the_decision", "before_the_claim"])
async def test_a_refresh_after_the_decision_aborts_the_assignment(
    window, committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    source, printer, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    item = await an_auto_job(committing_client, db_session, source)
    item_id, printer_id = item.id, printer.id
    originals = {
        (sc, "resolve_request"): sc.resolve_request,
        (scheduler_module, "snapshot_for_copy"): scheduler_module.snapshot_for_copy,
        (scheduler_module, "read_queue_occupancy"): scheduler_module.read_queue_occupancy,
    }

    if window == "after_the_decision":  # between the descriptor A and the promoted snapshot
        real = sc.resolve_request

        def decide_then_refresh(*args, **kwargs):
            decided = real(*args, **kwargs)
            pending.append(refreshed_behind_the_session(db_session, item_id, True))
            return decided

        pending: list = []
        monkeypatch.setattr(sc, "resolve_request", decide_then_refresh)
        real_snapshot = scheduler_module.snapshot_for_copy

        async def snapshot_after_the_refresh(db, row, *args, **kwargs):
            for change in pending:
                await change
            pending.clear()
            return await real_snapshot(db, row, *args, **kwargs)

        monkeypatch.setattr(scheduler_module, "snapshot_for_copy", snapshot_after_the_refresh)
    else:  # after the promoted snapshot A, under the lane, before the claim
        real_occupancy = scheduler_module.read_queue_occupancy

        async def refresh_then_occupancy(db, queue_id, **kwargs):
            await refreshed_behind_the_session(db, item_id, True)
            return await real_occupancy(db, queue_id, **kwargs)

        monkeypatch.setattr(scheduler_module, "read_queue_occupancy", refresh_then_occupancy)

    with pytest.raises(AutoPlacementConflict) as refused:
        await AutoQueueScheduler()._assign(db_session, item, printer)

    # The refresh rewrote the router row itself, so the placement's comparison of the locked row with what the
    # decision read refuses it first (G03 B, A17: "a source refresh" is item_changed); the swap basis re-read stays
    # the guard for a change that leaves the row alone (the declaration case below).
    assert refused.value.code == "item_changed"
    await db_session.commit()
    assert await children(db_session, item_id) == []
    db_session.expire_all()
    assert (await db_session.get(AutoQueueItem, item_id)).status == "pending"

    # The change stood in here for another session's commit; B is that commit (made again, idempotent).
    for (module, name), original in originals.items():
        monkeypatch.setattr(module, name, original)
    await refreshed_behind_the_session(db_session, item_id, True)
    await db_session.commit()
    db_session.expire_all()
    fresh = await db_session.get(AutoQueueItem, item_id)
    queued = await AutoQueueScheduler()._assign(db_session, fresh, await db_session.get(Printer, printer_id))
    assert sc.snapshot_fact(queued.source_snapshot) == sc.SourceFact(True)  # the next pass decides from B


async def test_a_new_source_declaration_behind_a_pre_fix_snapshot_aborts_the_assignment(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """The snapshot does not change at all -- only the effective fact the pre-fix snapshot falls back to does."""
    source, printer, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source.file_hash = sc_sha(source.file_path)
    source.swap_compatible = False  # a managed row's legacy False: the fallback answers False
    await db_session.commit()
    item = await an_auto_job(committing_client, db_session, source, fact="missing")
    snapshot, item_id, source_id, file_hash = dict(item.source_snapshot), item.id, source.id, source.file_hash
    real_occupancy = scheduler_module.read_queue_occupancy

    async def declare_then_occupancy(db, queue_id, **kwargs):
        record = {"version": 1, "value": True, "sha256": file_hash, "origin": "classifier"}
        await db.execute(  # the table, not the mapped class: the session's own row object must stay as it was
            update(LibraryFile.__table__)
            .where(LibraryFile.__table__.c.id == source_id)
            .values(swap_compatible=True, swap_source_declaration=record)
        )
        return await real_occupancy(db, queue_id, **kwargs)

    monkeypatch.setattr(scheduler_module, "read_queue_occupancy", declare_then_occupancy)

    with pytest.raises(AutoPlacementConflict) as refused:
        await AutoQueueScheduler()._assign(db_session, item, printer)

    assert refused.value.code == "swap_source_changed"
    await db_session.commit()
    assert await children(db_session, item_id) == []
    db_session.expire_all()
    assert (await db_session.get(AutoQueueItem, item_id)).source_snapshot == snapshot  # the snapshot never moved


def sc_sha(path: str) -> str:
    import hashlib
    from pathlib import Path

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def test_an_assignment_that_claimed_first_leaves_the_refresh_nothing(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    from backend.app.services import swap_job_refresh as jobs

    source, printer, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    item = await an_auto_job(committing_client, db_session, source)
    queued = await AutoQueueScheduler()._assign(db_session, item, printer)
    item_id, queued_id, source_id = item.id, queued.id, source.id
    await db_session.commit()
    db_session.expire_all()

    parent = await db_session.get(AutoQueueItem, item_id)
    assert (
        jobs._refusal(
            "auto_queue",
            parent,
            source_kind="library",
            source_id=source_id,
            sha256="0" * 64,
            blob=None,
            actor=None,
            rights=jobs.JobRights(read_all=True, update_all=True),
            scope=None,
        )
        == "status"
    )
    child = await db_session.get(PrintQueueItem, queued_id)
    assert child.source_auto_item_id == item_id
    # A promotion is a new incarnation, never the router row's own.
    assert sc.job_identity(child) and sc.job_identity(child) != sc.job_identity(parent)
    # ...and a job of its own: pending, never sent, its own link and its own blob -- it may take a declaration.
    from backend.app.models.queue_source import QueueSource

    blob = await db_session.get(QueueSource, child.queue_source_id)
    assert (
        jobs._refusal(
            "queue",
            child,
            source_kind="library",
            source_id=source_id,
            sha256=blob.sha256,
            blob=blob,
            actor=None,
            rights=jobs.JobRights(read_all=True, update_all=True),
            scope=None,
        )
        is None
    )


async def test_on_sqlite_an_assignment_never_waits_for_a_lane_another_writer_holds(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """The tick runs in one transaction that may already hold SQLite's writer. A refresh or a change_table claim
    holding the printer's lane in process waits for that writer (``claim_lanes``): an assignment that waited for the
    lane in turn would hold both for SQLite's busy timeout. It gives the printer up for this pass instead (T9 final
    review, I2)."""
    import asyncio

    from backend.app.services.queue_ops import _queue_claim_locks

    source, printer, *_ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    item = await an_auto_job(committing_client, db_session, source)
    item_id = item.id

    async with _queue_claim_locks[printer.id]:  # another writer holds this printer's lane
        with pytest.raises(AutoPlacementConflict) as refused:
            await asyncio.wait_for(AutoQueueScheduler()._assign(db_session, item, printer), 2)

    assert refused.value.code == "lane_busy"
    await db_session.commit()
    assert await children(db_session, item_id) == []
