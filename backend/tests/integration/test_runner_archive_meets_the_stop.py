"""The runner's execution-archive transaction meets a Stop with the real writers on both sides (G03 B, A16, L6(b);
acceptance TA32 F1; review B1-06).

The real ``_run_reprint_archive`` (hardware doubled, as ``test_reprint_creates_new_archive`` runs it) is stopped right
after the flush that writes its execution archive's ``queue_id`` and the job's ``archive_id`` -- it holds the job row
and the header ``FOR KEY SHARE`` (the foreign-key check). Against it the real Stop SQL phase
(``_stop_unbound_attempt``) and the scheduler's ``_cancel_item``, in both orders: the second one waits on the first
(the edge is checked by pid) and both end without ``40P01``. Under the former ``FOR UPDATE`` header read the two
crossed; that shape stays a negative control in ``test_pre_change_lock_shapes_deadlock``. Runs on PostgreSQL
(``DISPATCH_TEST_POSTGRES_URL``) and, runner first, on file-backed SQLite (whose writer the archive insert holds).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import archive_colors, queue_counters
from backend.tests.integration.lane_pairs import ANY_WAIT, a_waiter_appears, bypass_the_process_lane, held_after
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_reprint_creates_new_archive import run_reprint

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def deadlocked(outcome) -> bool:
    text = repr(outcome)
    return "40P01" in text or "deadlock" in text.lower()


@pytest.mark.parametrize("writer", ["stop", "cancel_item"])
@pytest.mark.parametrize("first", ["runner", "writer"])
async def test_the_runners_archive_and_a_stop_meet_without_a_deadlock(
    test_engine, db_session, tmp_path, monkeypatch, printer_factory, writer, first
):
    if first == "writer" and test_engine.dialect.name == "sqlite":
        pytest.skip("SQLite: the runner's archive insert already holds the writer, so the Stop cannot go inside first")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    arrived, flushed, runner_go = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_colors = archive_colors.apply_loaded_spool_colors

    async def colors(db, archive, printer_id, mapping):
        # Called right after the runner put the archive's queue_id and the job's archive_id in its session.
        arrived.set()
        if first == "writer":
            await asyncio.wait_for(runner_go.wait(), 20)  # the writer goes inside first
        await db.flush()  # the job row and the header's KEY SHARE are now held by the runner's transaction
        flushed.set()
        if first == "runner":
            await asyncio.wait_for(runner_go.wait(), 20)
        return await real_colors(db, archive, printer_id, mapping)

    monkeypatch.setattr(archive_colors, "apply_loaded_spool_colors", colors)
    # The user the harness's job names as having asked for the reprint: PostgreSQL checks the archive's key.
    from backend.app.models.user import User

    db_session.add(User(id=99, username="reprinter", password_hash="unused", role="admin", is_active=True))
    await db_session.commit()
    runner = asyncio.create_task(run_reprint(db_session, test_engine, tmp_path, monkeypatch, printer_factory))
    stopping = None
    try:
        reached = asyncio.ensure_future(arrived.wait())
        await asyncio.wait({reached, runner}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if not arrived.is_set():
            reached.cancel()
            pytest.fail(
                f"the runner never reached its archive flush: {runner.exception() if runner.done() else 'running'}"
            )
        async with factory() as db:
            claim = await db.scalar(select(PrintQueueItem).where(PrintQueueItem.status == "printing"))
            claim_id, queue_id, identity, token = claim.id, claim.queue_id, claim.recovery_identity, claim.started_at
            archive_id = claim.archive_id
        bypass_the_process_lane(test_engine, monkeypatch, queue_id)
        if first == "writer":
            writer_held, writer_go = held_after(monkeypatch, queue_counters, "halt_lane")

        async def stop():
            if writer == "stop":
                from backend.app.api.routes.print_queue import _stop_unbound_attempt

                return await _stop_unbound_attempt(
                    item_id=claim_id,
                    queue_id=queue_id,
                    identity=identity,
                    token=token,
                    archive_id=archive_id,
                    stop_sent=False,
                )
            from backend.app.services.print_scheduler import scheduler

            async with factory() as db:
                item = await db.get(PrintQueueItem, claim_id)
                return await scheduler._cancel_item(db, item, claim_started_at=token, queue_id=queue_id)

        if first == "runner":
            await asyncio.wait_for(flushed.wait(), 20)
            stopping = asyncio.create_task(stop())
            await a_waiter_appears(test_engine, factory, stopping, on=ANY_WAIT, behind=runner)
            runner_go.set()
        else:
            stopping = asyncio.create_task(stop())
            await asyncio.wait_for(writer_held.wait(), 20)
            runner_go.set()
            await a_waiter_appears(test_engine, factory, runner, on=ANY_WAIT, behind=stopping)
            writer_go.set()
        outcomes = await asyncio.wait_for(asyncio.gather(runner, stopping, return_exceptions=True), 60)
    finally:
        runner_go.set()
        if first == "writer" and "writer_go" in locals():
            writer_go.set()
        await asyncio.gather(runner, *([stopping] if stopping else []), return_exceptions=True)
    assert not any(deadlocked(outcome) for outcome in outcomes), outcomes
    assert not isinstance(outcomes[1], BaseException), outcomes
    async with factory() as db:
        assert (await db.get(PrintQueueItem, claim_id)).status == "cancelled"
