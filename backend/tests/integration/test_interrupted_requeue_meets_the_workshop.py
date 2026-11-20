"""The interrupted dispatch's requeue (``_settle``) restoring a job's link meets the Workshop's filing of the same prints,
both orders (G03 B, A24 / A27; acceptance TA30 for ``interrupted_dispatch._settle``).

A dispatch that ended with its process leaves the job printing on its execution archive X, captured from archive A.
The requeue returns it to waiting and restores its link to A (the stamped incarnation), A ``FOR KEY SHARE`` before
the job row. ``POST /projects/{id}/add-archives`` of ``[A, X]`` takes both ``FOR UPDATE``. Whichever goes first, the
other waits on an archive, and both commit.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import interrupted_dispatch, queue_source_link
from backend.tests.integration.lane_pairs import bypass_the_process_lane, held_after
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_interrupted_dispatch import an_interrupted_job, owner  # noqa: F401
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def an_order(committing_client) -> int:
    created = await committing_client.post("/api/v1/projects/", json={"name": "Filed"})
    assert created.status_code in (200, 201), created.text
    return created.json()["id"]


def file(client, order_id, original_id, execution_id):
    return client.post(f"/api/v1/projects/{order_id}/add-archives", json={"archive_ids": [original_id, execution_id]})


async def requeue(printer_id):
    return await interrupted_dispatch.recover_interrupted_dispatches(printer_id)


async def outcome(factory, order_id, original_id, execution_id, item_id):
    async with factory() as db:
        item = await db.get(PrintQueueItem, item_id)
        a = await db.get(PrintArchive, original_id)
        x = await db.get(PrintArchive, execution_id)
    assert (item.status, item.archive_id) == ("pending", original_id), "requeued, its link restored to the original"
    assert (a.project_id, x.project_id) == (order_id, order_id)


async def test_the_filing_meeting_a_requeue_that_holds_the_original_waits_on_an_archive(
    test_engine, db_session, printer_factory, archive_factory, committing_client, telemetry, owner, monkeypatch
):
    printer, queue, archive, item, source = await an_interrupted_job(db_session, printer_factory, archive_factory)
    order_id = await an_order(committing_client)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue.id)
    reached, release = held_after(monkeypatch, queue_source_link, "lock_parent")
    requeuing = asyncio.create_task(requeue(printer.id))
    filing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        filing = asyncio.create_task(file(committing_client, order_id, source.id, archive.id))
        await asyncio.sleep(0.5)
        assert not filing.done(), "the filing waits for the requeue's archives"
        release.set()
        assert await asyncio.wait_for(requeuing, 20) == [interrupted_dispatch.REQUEUED]
        response = await asyncio.wait_for(filing, 20)
    finally:
        release.set()
        await asyncio.gather(requeuing, *([filing] if filing else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, order_id, source.id, archive.id, item.id)


async def test_a_requeue_meeting_the_filing_that_holds_the_archives_waits_then_restores(
    test_engine, db_session, printer_factory, archive_factory, committing_client, telemetry, owner, monkeypatch
):
    from backend.app.api.routes import projects

    printer, queue, archive, item, source = await an_interrupted_job(db_session, printer_factory, archive_factory)
    order_id = await an_order(committing_client)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    bypass_the_process_lane(test_engine, monkeypatch, queue.id)
    reached, release = held_after(monkeypatch, projects, "lock_prints")
    filing = asyncio.create_task(file(committing_client, order_id, source.id, archive.id))
    requeuing = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        requeuing = asyncio.create_task(requeue(printer.id))
        await asyncio.sleep(0.5)
        assert not requeuing.done(), "the requeue waits for the filing's archives"
        release.set()
        response = await asyncio.wait_for(filing, 20)
        assert await asyncio.wait_for(requeuing, 20) == [interrupted_dispatch.REQUEUED]
    finally:
        release.set()
        await asyncio.gather(filing, *([requeuing] if requeuing else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    await outcome(factory, order_id, source.id, archive.id, item.id)
