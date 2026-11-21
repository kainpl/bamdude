"""TB12: the unsent writer serializes at the actual lane and archive guards."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from backend.app.api.routes import auto_queue, print_queue
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueUnsentReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import (
    archive_delete_scope,
    archive_write_scope,
    auto_queue_unsent_return,
    print_state_recovery_preview,
    print_state_recovery_store,
    queue_counters,
    queue_ops,
    queue_sources,
)
from backend.app.services.archive import ArchiveService
from backend.app.services.auto_queue_scheduler import AutoPlacementConflict
from backend.app.utils.failure_reasons import DISPATCH_UPLOAD_FAILED
from backend.tests.integration.lane_pairs import (
    NoProcessLock,
    bypass_the_process_lane,
    held_after,
    held_inside,
    postgres_edge,
)
from backend.tests.integration.test_auto_queue_unsent_return import (  # noqa: F401 - fixtures
    assignment,
    clean_spool_state,
    prepare_claim,
    sessions,
    test_engine,
)
from backend.tests.integration.test_dispatch_without_original import scheduler_with_captured_dispatch
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401


async def end_attempt(sessions, scheduler, assignment, token, archive_id, identity):
    async with sessions() as db:
        return await scheduler._fail_attempt(
            db,
            item_id=assignment.job,
            queue_id=assignment.queue,
            claim_started_at=token,
            message="Upload refused",
            code=DISPATCH_UPLOAD_FAILED,
            sent=False,
            execution_archive_id=archive_id,
            execution_archive_identity=identity,
            unsent_attempt=auto_queue_unsent_return.UnsentAttempt(queue_sources.current_epoch(), archive_id, identity),
        )


@pytest.mark.parametrize("other", ["cancel", "recovery", "delete_archive", "stop"])
@pytest.mark.parametrize("return_first", [True, False])
async def test_return_and_competing_writer_use_the_exact_postgres_guard(
    assignment,
    test_engine,
    sessions,
    committing_client,
    monkeypatch,
    telemetry,
    other,
    return_first,
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("requires the advisory-lock backend")
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    token, (archive_id, identity) = await prepare_claim(sessions, assignment, archive_phase="prepared")
    bypass_the_process_lane(test_engine, monkeypatch, assignment.queue)
    monkeypatch.setitem(archive_write_scope._archive_locks, archive_id, NoProcessLock())
    preview = None
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        job_identity = row.recovery_identity
        if other in {"recovery", "delete_archive"}:
            archive = await db.get(PrintArchive, archive_id)
            archive.status, archive.failure_reason, archive.completed_at = "failed", DISPATCH_UPLOAD_FAILED, token
            if other == "delete_archive":
                # Before the copied archive is linked, its own guard is all the attempt and hard-delete share.
                row.archive_id = None
            else:
                token -= timedelta(hours=2)
                row.started_at = archive.started_at = token
                archive.completed_at = token + timedelta(hours=1)
                archive.queue_id = assignment.queue
            await db.commit()
            if other == "recovery":
                preview = await print_state_recovery_store.make_preview(
                    db, assignment.printer, "item", assignment.job, "align_terminal_item", "user:1"
                )

    async def competitor():
        if other == "cancel":
            response = await committing_client.delete(f"/api/v1/auto-queue/{assignment.auto}")
            assert response.status_code == 200, response.text
            return response.json()["status"]
        if other == "stop":
            return await print_queue._stop_unbound_attempt(
                item_id=assignment.job,
                queue_id=assignment.queue,
                identity=job_identity,
                token=token,
                archive_id=archive_id,
                stop_sent=False,
            )
        if other == "recovery":
            payload = print_state_recovery_preview.verify(preview["preview_token"])
            try:
                return await print_state_recovery_store.apply_preview(sessions, payload, "user:1", "Operator")
            except ValueError as exc:
                assert str(exc) == "recovery_changed"
                return "changed"
        async with sessions() as db:
            return await ArchiveService(db).delete_archive(archive_id)

    async def returning():
        return await end_attempt(sessions, scheduler, assignment, token, archive_id, identity)

    if return_first:
        reached, release = held_after(monkeypatch, auto_queue_unsent_return, "return_unsent")
    elif other == "cancel":
        reached, release = held_after(monkeypatch, auto_queue, "_cancel_promoted_rows")
    elif other == "stop":
        reached, release = held_after(monkeypatch, queue_counters, "halt_lane")
    elif other == "recovery":
        reached, release = held_after(monkeypatch, print_state_recovery_store, "load_active_archive_for_write")
    else:
        reached, release = held_inside(monkeypatch, archive_delete_scope, "archive_delete_scope")
    first = asyncio.create_task(returning() if return_first else competitor(), name="first writer")
    second = None
    try:
        await asyncio.wait_for(reached.wait(), 15)
        second = asyncio.create_task(competitor() if return_first else returning(), name="waiting writer")
        await postgres_edge(
            sessions,
            second,
            queue_id=assignment.queue if other == "cancel" else None,
            archive_id=None if other == "cancel" else archive_id,
        )
        release.set()
        one, two = await asyncio.wait_for(asyncio.gather(first, second), 20)
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    failure, other_result = (one, two) if return_first else (two, one)
    assert failure.returned is return_first
    if other == "stop":
        assert other_result == ("changed" if return_first else "stopped")
    elif other == "cancel":
        assert other_result == "cancelled"
    elif other == "recovery":
        assert (other_result == "changed") is return_first
    else:
        assert other_result is True
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        auto = await db.get(AutoQueueItem, assignment.auto)
        assert bool(row.returned_receipt_identity) is return_first
        assert await db.scalar(select(func.count(AutoQueueUnsentReturn.id))) == int(return_first)
        assert row.status == ("cancelled" if other == "stop" and not return_first else "failed")
        if auto is not None:
            assert auto.unsent_return_count == int(return_first)
        else:
            assert other == "cancel" and return_first


async def test_sqlite_router_holding_the_writer_never_waits_for_the_return_lane(
    assignment, test_engine, sessions, monkeypatch
):
    if test_engine.dialect.name != "sqlite":
        pytest.skip("SQLite writer ordering")
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    token, (archive_id, identity) = await prepare_claim(sessions, assignment, archive_phase="prepared")
    reached, release = held_after(monkeypatch, auto_queue_unsent_return, "return_unsent")
    async with sessions() as writer:
        await writer.execute(text("BEGIN IMMEDIATE"))
        auto = await writer.get(AutoQueueItem, assignment.auto)
        printer = await writer.get(Printer, assignment.printer)
        returned = asyncio.create_task(end_attempt(sessions, scheduler, assignment, token, archive_id, identity))
        try:
            async with asyncio.timeout(5):
                while not queue_ops._queue_claim_locks[assignment.queue].locked():
                    await asyncio.sleep(0.01)
            assert not returned.done()
            with pytest.raises(AutoPlacementConflict, match="lane_busy"):
                await asyncio.wait_for(assignment.router._assign(writer, auto, printer), 2)
        finally:
            await writer.rollback()
            release.set()
        await asyncio.wait_for(reached.wait(), 5)
        assert (await asyncio.wait_for(returned, 15)).returned
