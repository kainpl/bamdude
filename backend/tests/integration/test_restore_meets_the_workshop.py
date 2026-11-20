"""The deferral's source restore meets the Workshop's filing of the same prints, both orders (G03 B, A10 / A24 / A27;
acceptance TA30).

A job captured from archive A, claimed and re-pointed to its execution archive X, is sent back to waiting by the real
``defer_claim`` with its source restored: it takes the archives ascending (A ``KEY SHARE``, X ``NO KEY UPDATE``) and
then the job row. B is the real ``POST /projects/{id}/add-archives`` of ``[A, X]`` (``lock_prints``: ``FOR UPDATE``,
ascending). Whichever goes first, the other waits on an archive row -- never the deferral on B after its job row -- and
both commit; with A < X and with A > X.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_source_link, swap_compatibility
from backend.tests.integration.lane_pairs import (
    ROW_WAIT,
    a_waiter_appears,
    bypass_the_process_lane,
    held_after,
    pending_rows,
)
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T0 = datetime(2026, 11, 20, 10, 0, 0)
NOT_STARTED = {swap_compatibility.CARRIER_KEY: {"v": 1}}


async def a_restorable_attempt(db_session, printer_factory, archive_factory, committing_client, *, original_first):
    """Returns (order, queue, A, X, job). ``original_first``: A gets the lower id."""
    queue_id, _ = await pending_rows(db_session, printer_factory, count=0)
    made = {}
    for name in ("A", "X") if original_first else ("X", "A"):
        if name == "A":
            made[name] = await archive_factory(queue_id, status="completed")
        else:
            # The execution archive records that our swap start did not run (the deferral is no failure).
            made[name] = await archive_factory(queue_id, status="printing", extra_data=NOT_STARTED)
    original, execution = made["A"], made["X"]
    job = PrintQueueItem(
        queue_id=queue_id,
        position=0,
        status="printing",
        started_at=T0,
        archive_id=execution.id,
        source_snapshot={
            "version": 1,
            "provenance": {"kind": "archive", "id": original.id, "incarnation": original.recovery_identity},
        },
    )
    db_session.add(job)
    await db_session.flush()
    header = await db_session.get(PrinterQueue, queue_id)
    header.status, header.current_item_id = "printing", job.id
    await db_session.commit()
    created = await committing_client.post("/api/v1/projects/", json={"name": "Filed"})
    assert created.status_code in (200, 201), created.text
    return created.json()["id"], queue_id, original, execution, job.id


async def defer(factory, job_id, queue_id, original, execution):
    from backend.app.services.filament_deferred import defer_claim

    async with factory() as db:
        return await defer_claim(
            db,
            item_id=job_id,
            started_at=T0,
            queue_id=queue_id,
            reason="feed_settle_timeout",
            restore_source=True,
            source_archive_id=original.id,
            source_archive_identity=original.recovery_identity,
            execution_archive_id=execution.id,
        )


def file(client, order_id, original, execution):
    return client.post(f"/api/v1/projects/{order_id}/add-archives", json={"archive_ids": [original.id, execution.id]})


async def outcome(factory, order_id, job_id, original, execution):
    async with factory() as db:
        job = await db.get(PrintQueueItem, job_id)
        a = await db.get(PrintArchive, original.id)
        x = await db.get(PrintArchive, execution.id)
    assert (job.status, job.archive_id) == ("pending", original.id), "the source restored to the proven original"
    assert (a.project_id, x.project_id) == (order_id, order_id)


@pytest.mark.parametrize("original_first", [True, False], ids=["A<X", "A>X"])
async def test_the_filing_meeting_the_restore_waits_on_an_archive_row(
    original_first, test_engine, db_session, printer_factory, archive_factory, committing_client, monkeypatch
):
    order_id, queue_id, original, execution, job_id = await a_restorable_attempt(
        db_session, printer_factory, archive_factory, committing_client, original_first=original_first
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    # The deferral stopped after its original's KEY SHARE: with A < X before X, with A > X after both.
    reached, release = held_after(monkeypatch, queue_source_link, "lock_parent")
    deferring = asyncio.create_task(defer(factory, job_id, queue_id, original, execution))
    filing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        filing = asyncio.create_task(file(committing_client, order_id, original, execution))
        await a_waiter_appears(test_engine, factory, filing, on=ROW_WAIT)
        release.set()
        assert (await asyncio.wait_for(deferring, 20)).owned
        response = await asyncio.wait_for(filing, 20)
    finally:
        release.set()
        await asyncio.gather(deferring, *([filing] if filing else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, order_id, job_id, original, execution)


@pytest.mark.parametrize("original_first", [True, False], ids=["A<X", "A>X"])
async def test_the_restore_meeting_the_filing_waits_on_an_archive_row_before_its_job(
    original_first, test_engine, db_session, printer_factory, archive_factory, committing_client, monkeypatch
):
    from backend.app.api.routes import projects

    order_id, queue_id, original, execution, job_id = await a_restorable_attempt(
        db_session, printer_factory, archive_factory, committing_client, original_first=original_first
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue_id)
    reached, release = held_after(monkeypatch, projects, "lock_prints")
    filing = asyncio.create_task(file(committing_client, order_id, original, execution))
    deferring = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        deferring = asyncio.create_task(defer(factory, job_id, queue_id, original, execution))
        await a_waiter_appears(test_engine, factory, deferring, on=ROW_WAIT)
        release.set()
        response = await asyncio.wait_for(filing, 20)
        assert (await asyncio.wait_for(deferring, 20)).owned
    finally:
        release.set()
        await asyncio.gather(filing, *([deferring] if deferring else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, order_id, job_id, original, execution)
