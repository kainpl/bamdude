"""A writer locks a job row only in the queue whose lane it holds (G03 B, L1; review B1-02).

A writer reads a row in queue Q1 and takes Q1's lane; meanwhile the row moved to Q2, where another writer holds Q2's
lane and the row itself. The row is addressed by (id, the queue it was read in) before ``FOR …``, so the stale writer
never asks for that lock: it refuses while the Q2 holder still holds, instead of waiting behind a lane it does not
hold. Checked for the edit's apply, the light writers' ``lock_named_rows`` and the claim's ``lock_judged_row``; the
negative control is the former statement by id alone, which waits behind the holder. PostgreSQL only: on SQLite
every lane holder already serialises on the writer.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import queue_ops
from backend.app.services.queue_row_basis import row_basis
from backend.tests.integration.lane_pairs import ROW_WAIT, bypass_the_process_lane, pending_rows, postgres_edge
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def postgres_only(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks; SQLite serialises every lane holder on its writer")


@pytest.fixture
async def moved(postgres_only, test_engine, db_session, printer_factory, monkeypatch):
    """One job read in Q1 -- its row as read, for the stale writers -- then moved to Q2 and committed."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    q1, (item_id,) = await pending_rows(db_session, printer_factory, count=1)
    q2, _ = await pending_rows(db_session, printer_factory, count=0)
    for lane in (q1, q2):
        bypass_the_process_lane(test_engine, monkeypatch, lane)
    async with factory() as db:
        as_read = await db.get(PrintQueueItem, item_id)
        basis = row_basis(as_read)
    async with factory() as db, queue_ops.claim_lanes(db, [q1, q2]):
        row = await db.get(PrintQueueItem, item_id)
        row.queue_id = q2
        await db.commit()
    return {"factory": factory, "q1": q1, "q2": q2, "item": item_id, "as_read": as_read, "basis": basis}


async def held_in_q2(case):
    """Q2's lane and the moved row, held by another writer."""
    holder = case["factory"]()
    await holder.__aenter__()
    lanes = queue_ops.claim_lanes(holder, [case["q2"]])
    await lanes.__aenter__()
    await holder.scalar(
        select(PrintQueueItem.id).where(PrintQueueItem.id == case["item"]).with_for_update(key_share=True)
    )
    return holder, lanes


async def let_go(holder, lanes):
    await holder.rollback()
    await lanes.__aexit__(None, None, None)
    await holder.__aexit__(None, None, None)


async def refuses_while_q2_is_held(case, writer):
    holder, lanes = await held_in_q2(case)
    try:
        return await asyncio.wait_for(writer(), 10)  # a wait behind the Q2 holder would last until let_go
    finally:
        await let_go(holder, lanes)


async def test_a_stale_edit_refuses_without_waiting_for_the_q2_holder(moved):
    from backend.app.services.queue_edit_apply import apply_edits, plan_edit

    case = moved
    plan = plan_edit(
        case["as_read"], {"layer_inspect": True}, basis=case["basis"], target_queue_id=None, facts={case["q1"]: None}
    )

    async def edit():
        return await apply_edits([plan], set_field=setattr, all_or_nothing=True)

    outcome = await refuses_while_q2_is_held(case, edit)
    assert (outcome.applied, outcome.changed) == ([], [case["item"]])


async def test_a_stale_light_writer_finds_nothing_without_waiting_for_the_q2_holder(moved):
    case = moved

    async def light_writer():
        async with case["factory"]() as db, queue_ops.claim_lanes(db, [case["q1"]]):
            return await queue_ops.lock_named_rows(db, {case["item"]: case["q1"]})

    assert await refuses_while_q2_is_held(case, light_writer) == {}


async def test_a_stale_claim_refuses_without_waiting_for_the_q2_holder(moved):
    from backend.app.services.print_scheduler import lock_judged_row

    case = moved

    async def claim():
        async with case["factory"]() as db, queue_ops.claim_lanes(db, [case["q1"]]):
            return await lock_judged_row(db, item_id=case["item"], queue_id=case["q1"], basis=case["basis"])

    assert await refuses_while_q2_is_held(case, claim) == (None, "item_changed")


async def test_the_former_lock_by_id_alone_waited_behind_the_q2_holder(moved):
    """Negative control: the statement the writers used before -- the row by id under Q1's lane."""
    case = moved
    holder, lanes = await held_in_q2(case)

    async def by_id_alone():
        async with case["factory"]() as db, queue_ops.claim_lanes(db, [case["q1"]]):
            await db.execute(text("SELECT id FROM print_queue WHERE id = :id FOR NO KEY UPDATE"), {"id": case["item"]})
            await db.rollback()

    waiting = asyncio.create_task(by_id_alone())
    try:
        await postgres_edge(case["factory"], waiting, on=ROW_WAIT, behind=asyncio.current_task())
        assert not waiting.done()
    finally:
        await let_go(holder, lanes)
    await asyncio.wait_for(waiting, 10)
