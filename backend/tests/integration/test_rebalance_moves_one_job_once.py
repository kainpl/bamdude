"""Two runs that decide to move one job: exactly one converts it (Codex review r1, RB-R1).

Every final check before a move can pass in both runs — the tick's and a button's — and the
second conversion would add another k − 1 companions and overwrite where the job came from.
What makes the move one thing is the row read again UNDER its lock, after every long await
(the copy, the publication), and compared with what the decision read.

These run on a file-backed SQLite database whose sessions hold connections of their own — the
suite's in-memory engine shares one connection between every session, so a commit in one is no
other's evidence. ``test_rebalance_job_feasibility_on_postgres.py`` runs the same bodies on a
disposable PostgreSQL server.
"""

import asyncio

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.services import queue_rebalance
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 — fixtures by name
    _live,
    clean_spool,
    petg_farm,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def test_engine(monkeypatch, tmp_path):
    """A database file, so two sessions are two connections and a commit is the other's evidence."""
    from backend.app.core import database as database_module
    from backend.app.core.database import Base, configure_sqlite_connection, import_all_models

    import_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "moves.sqlite"))
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database_module, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    await engine.dispose()
    await asyncio.sleep(0.1)


async def _rows(test_engine) -> list[AutoQueueItem]:
    async with AsyncSession(test_engine, expire_on_commit=False) as fresh:
        return list((await fresh.execute(select(AutoQueueItem).order_by(AutoQueueItem.id))).scalars().all())


async def test_a_run_that_moved_the_job_after_this_ones_check_wins(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """The probe of the review: this run passed its final check and is publishing the copy when
    another run, on a session of its own, moves the same job through the real writers. This run
    converts nothing and creates nothing — one job of 6 parts on a 2-part plate is 3 rows."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1, alt_hooks=2)
    line_id, item_id = farm.line.id, farm.item.id
    real_publish = queue_rebalance.publish_staged
    other: dict = {}

    async def another_run_first(*args, **kwargs):
        if not other:
            other["started"] = True
            async with AsyncSession(test_engine, expire_on_commit=False) as session:
                other["result"] = await queue_rebalance.rebalance(session, line_ids=[line_id], force=True)
                await session.commit()
        return await real_publish(*args, **kwargs)

    monkeypatch.setattr(queue_rebalance, "publish_staged", another_run_first)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[line_id], force=True)
    await db_session.commit()

    won = other["result"]
    assert (won.converted, won.created) == (1, 2)
    assert (result.converted, result.created, result.skipped) == (0, 0, [(item_id, "eligibility_changed")])
    rows = await _rows(test_engine)
    assert len(rows) == 3
    moved = next(row for row in rows if row.id == item_id)
    assert (moved.target_model, moved.rebalanced_from_model, moved.library_file_id) == ("P1S", "P2S", farm.alt_file.id)


async def test_a_run_that_converts_first_wins_over_one_that_already_checked(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """The other order: the second run has copied the file and passed its final check, and is
    waiting on its publication, when this run converts the job and commits. The second run then
    finds the job is no longer the one it decided on, and moves nothing."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1, alt_hooks=2)
    line_id, item_id = farm.line.id, farm.item.id
    real_publish = queue_rebalance.publish_staged
    main = asyncio.current_task()
    second_checked = asyncio.Event()
    second_may_go = asyncio.Event()
    second: dict = {}

    async def the_second_run():
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            second["result"] = await queue_rebalance.rebalance(session, line_ids=[line_id], force=True)
            await session.commit()

    async def interleaved(*args, **kwargs):
        if asyncio.current_task() is main:
            if "task" not in second:
                second["task"] = asyncio.create_task(the_second_run())
                await second_checked.wait()
        else:
            second_checked.set()
            await second_may_go.wait()
        return await real_publish(*args, **kwargs)

    monkeypatch.setattr(queue_rebalance, "publish_staged", interleaved)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[line_id], force=True)
        await db_session.commit()
        second_may_go.set()
        await second["task"]

    assert (result.converted, result.created) == (1, 2)
    lost = second["result"]
    assert (lost.converted, lost.created, lost.skipped) == (0, 0, [(item_id, "eligibility_changed")])
    assert len(await _rows(test_engine)) == 3


async def test_the_row_stays_claimed_until_the_conversion_is_committed(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """The claim is a lock, not only a read: once this run has read the row under it, a cancel —
    which takes no order lock, only the row — waits for the conversion's commit instead of
    deleting the row between the read and the write (the ORM's UPDATE would then match nothing).
    It deletes the moved job afterwards, which is what the operator asked for."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    item_id = farm.item.id
    real_claim = queue_rebalance._claim_row
    cancel: dict = {}

    async def cancel_it():
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            await session.delete(await session.get(AutoQueueItem, item_id))
            await session.commit()

    async def claim_then_cancel(db, item, recheck):
        why = await real_claim(db, item, recheck)
        cancel["task"] = asyncio.create_task(cancel_it())
        await asyncio.sleep(0.5)
        cancel["waited"] = not cancel["task"].done()
        return why

    monkeypatch.setattr(queue_rebalance, "_claim_row", claim_then_cancel)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    await db_session.commit()
    await cancel["task"]

    assert cancel["waited"]
    assert (result.converted, result.skipped) == (1, [])
    assert await _rows(test_engine) == []


@pytest.mark.parametrize("change", ["assigned", "cancelled"])
async def test_an_assignment_or_cancel_after_the_check_stops_the_move(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, change
):
    """Between the final check and the write the router assigns the job (its own claim
    statement), or an operator cancels it (an un-routed pending row is deleted). The move writes
    nothing over it."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1, alt_hooks=2)
    item_id = farm.item.id
    real_publish = queue_rebalance.publish_staged

    async def meanwhile(*args, **kwargs):
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            if change == "assigned":
                await session.execute(
                    update(AutoQueueItem)
                    .where(
                        AutoQueueItem.id == item_id,
                        AutoQueueItem.status == "pending",
                        AutoQueueItem.cancelled_at.is_(None),
                    )
                    .values(status="assigned")
                )
            else:
                await session.delete(await session.get(AutoQueueItem, item_id))
            await session.commit()
        return await real_publish(*args, **kwargs)

    monkeypatch.setattr(queue_rebalance, "publish_staged", meanwhile)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    await db_session.commit()

    expected = "already_assigned" if change == "assigned" else "not_found"
    assert (result.converted, result.created, result.skipped) == (0, 0, [(item_id, expected)])
    rows = await _rows(test_engine)
    if change == "assigned":
        assert [(row.id, row.status, row.target_model, row.rebalanced_at) for row in rows] == [
            (item_id, "assigned", "P2S", None)
        ]
    else:
        assert rows == []
