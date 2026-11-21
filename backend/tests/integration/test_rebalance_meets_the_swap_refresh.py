"""A rebalance move onto target file F meets a swap-declaration refresh for F (G03 B, A28 / A31; acceptance TA35).

The move takes F ``FOR KEY SHARE`` inside its blob's pin, before its order and router rows (A28). A refresh request
for F naming the router row being moved -- still F0's job, its revision from before the move -- is refused while the
move holds F, before the refresh takes any lock: its selection is validated against F's own jobs first
(``selection_invalid``), so it never waits on the move and the move converts. (The spec's stale-revision pair that
locks the moved row cannot arise: a refresh locks only F's own jobs, and the move holds none of them. The F lock
interaction itself -- ``FOR UPDATE`` against the move's ``KEY SHARE``, and r5's cycle -- is pinned by
``test_pre_change_lock_shapes_deadlock`` and TA35 (d)(e) in ``test_rebalance_target_file_changed_under_the_pin``.)
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.services import queue_rebalance, swap_job_refresh as jobs
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 -- fixtures by name
    _live,
    clean_spool,
    petg_farm,
)
from backend.tests.integration.test_swap_declaration_service import ALL, a_user, actor_of, declare
from backend.tests.integration.test_swap_job_refresh import QUEUE_ALL

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def a_declared_target(factory, farm):
    import hashlib
    from pathlib import Path

    from sqlalchemy import update

    from backend.app.models.library import LibraryFile
    from backend.app.services import swap_source_declarations as decl

    user = await a_user(factory)
    target = farm.alt_file.id
    # A verified content hash, as an upload or a scan leaves it -- the declaration is bound to these bytes.
    digest = hashlib.sha256(Path(farm.alt_file.file_path).read_bytes()).hexdigest()
    async with factory() as db:
        await db.execute(
            update(LibraryFile).where(LibraryFile.id == target).values(file_hash=digest, is_external=False)
        )
        await db.commit()
    seen = await decl.inspect("library", target, actor=actor_of(user), rights=ALL)
    await declare("library", target, seen, user, value=False)
    # The listing a refresh request is made from: its declaration and source revision.
    seen = await jobs.candidates("library", target, actor=actor_of(user), rights=ALL, job_rights=QUEUE_ALL)
    async with factory() as db:
        before = jobs.job_revision("auto", await db.get(AutoQueueItem, farm.item.id))
    return user, target, seen, before


async def the_refresh(user, target, seen, item_id, revision):
    return await jobs.refresh(
        "library",
        target,
        declaration_id=seen["declaration_id"],
        source_revision=seen["source_revision"],
        jobs=[jobs.JobRef("auto", item_id, revision)],
        operation_id=str(uuid.uuid4()),
        actor=actor_of(user),
        rights=ALL,
        job_rights=QUEUE_ALL,
    )


async def the_move(db_session, farm):
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        return await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)


async def refused(task):
    try:
        await asyncio.wait_for(task, 30)
    except HTTPException as exc:
        return exc
    raise AssertionError("the refresh of a moved job must be refused")


async def converted(factory, farm):
    async with factory() as db:
        row = await db.scalar(select(AutoQueueItem).where(AutoQueueItem.id == farm.item.id))
    return row.library_file_id == farm.alt_file.id


async def test_a_refresh_naming_the_moving_row_is_refused_without_waiting_on_the_move(
    test_engine, db_session, printer_factory, tmp_path, monkeypatch, clean_spool
):
    from backend.app.services import order_resources

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    user, target, seen, before = await a_declared_target(factory, farm)
    # The move stopped right after its target file's KEY SHARE: the next thing it does is read the order's rules.
    reached, release = asyncio.Event(), asyncio.Event()
    real = order_resources.policy_for

    async def held(*args, **kwargs):
        if not reached.is_set():
            reached.set()
            await asyncio.wait_for(release.wait(), 20)
        return await real(*args, **kwargs)

    monkeypatch.setattr(order_resources, "policy_for", held)
    moving = asyncio.create_task(the_move(db_session, farm))
    refreshing = None
    try:
        await asyncio.wait_for(reached.wait(), 15)
        refreshing = asyncio.create_task(the_refresh(user, target, seen, farm.item.id, before))
        # Answered while the move still holds F: the refresh took no lock the move holds.
        answer = await refused(refreshing)
        assert not moving.done()
        release.set()
        result = await asyncio.wait_for(moving, 30)
    finally:
        release.set()
        await asyncio.gather(moving, *([refreshing] if refreshing else []), return_exceptions=True)
    assert result.converted == 1, result.skipped
    assert (answer.status_code, answer.detail["error"]) == (422, "selection_invalid")
    assert await converted(factory, farm)
