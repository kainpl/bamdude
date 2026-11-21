"""B2-01: archive filing and terminal return converge without holding conflicting guards."""

import asyncio

import pytest
from sqlalchemy import event, func, select, text

from backend.app.api.routes import archives, print_queue
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueUnsentReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.project import Project
from backend.app.services import archive_write_scope, auto_queue_unsent_return
from backend.app.services.auto_queue_placement_basis import placement_basis
from backend.tests.integration.lane_pairs import ROW_WAIT, NoProcessLock, held_after, postgres_edge
from backend.tests.integration.test_auto_queue_unsent_return import (  # noqa: F401 - fixtures
    assignment,
    clean_spool_state,
    prepare_claim,
    sessions,
    test_engine,
)
from backend.tests.integration.test_dispatch_without_original import scheduler_with_captured_dispatch
from backend.tests.integration.test_unsent_return_meets_other_writers import end_attempt


@pytest.mark.parametrize("first", ["patch", "return", "patch_then_stop"])
async def test_ordered_archive_patch_and_unsent_return_share_the_order_without_a_cycle(
    assignment, test_engine, sessions, committing_client, monkeypatch, first
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory and row locks")
    token, (archive_id, archive_identity) = await prepare_claim(sessions, assignment, archive_phase="prepared")
    async with sessions() as db:
        project = Project(name="Order for the refused job", status="active")
        db.add(project)
        await db.flush()
        job = await db.get(PrintQueueItem, assignment.job)
        auto = await db.get(AutoQueueItem, assignment.auto)
        archive = await db.get(PrintArchive, archive_id)
        project_id, job_identity = project.id, job.recovery_identity
        archive.project_id = auto.project_id = job.project_id = project_id
        job.placement_basis = placement_basis(job)
        await db.commit()

    # Simulate separate processes while keeping the PostgreSQL guards intact.
    monkeypatch.setitem(archive_write_scope._archive_locks, archive_id, NoProcessLock())
    if first == "return":
        reached, release = held_after(monkeypatch, auto_queue_unsent_return, "lock_order_for_return")
    else:
        reached, release = held_after(monkeypatch, archives, "_lock_exit_orders")
    states = []

    @event.listens_for(test_engine.sync_engine, "handle_error")
    def record_sqlstate(ctx):
        states.append(getattr(ctx.original_exception, "sqlstate", None))

    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)

    async def patch():
        return await committing_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": project_id})

    async def finish():
        return await end_attempt(sessions, scheduler, assignment, token, archive_id, archive_identity)

    leader = asyncio.create_task(finish() if first == "return" else patch())
    follower = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        follower = asyncio.create_task(patch() if first == "return" else finish())
        edge = await postgres_edge(sessions, follower, on=ROW_WAIT)
        async with sessions() as db:
            assert not await db.scalar(
                text("SELECT count(*) FROM pg_locks WHERE pid = :pid AND locktype = 'advisory' AND granted"),
                {"pid": edge.pid},
            )
        if first == "patch_then_stop":
            stopped = await asyncio.wait_for(
                print_queue._stop_unbound_attempt(
                    item_id=assignment.job,
                    queue_id=assignment.queue,
                    identity=job_identity,
                    token=token,
                    archive_id=archive_id,
                    stop_sent=False,
                ),
                5,
            )
            assert stopped == "stopped"
        release.set()
        leading, following = await asyncio.wait_for(asyncio.gather(leader, follower), 20)
    finally:
        release.set()
        await asyncio.gather(leader, *([follower] if follower else []), return_exceptions=True)

    outcome, response = (leading, following) if first == "return" else (following, leading)
    assert response.status_code == 200, response.text
    assert "40P01" not in states, states
    assert ("55P03" in states) is (first != "return")
    cancelled = first == "patch_then_stop"
    assert (outcome.written, outcome.returned) == (not cancelled, not cancelled)
    async with sessions() as db:
        job = await db.get(PrintQueueItem, assignment.job)
        auto = await db.get(AutoQueueItem, assignment.auto)
        archive = await db.get(PrintArchive, archive_id)
        assert job.status == archive.status == ("cancelled" if cancelled else "failed")
        assert (job.returned_receipt_identity is not None) is (not cancelled)
        assert (auto.status, auto.unsent_return_count) == (("assigned", 0) if cancelled else ("pending", 1))
        assert await db.scalar(select(func.count(AutoQueueUnsentReturn.id))) == int(not cancelled)
