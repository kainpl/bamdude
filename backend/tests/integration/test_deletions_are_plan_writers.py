"""An archive's and an order's deletions are plan writers for the job rows they write (G03 B, A19 / A22).

The footprint -- the lanes of the job rows the deletion writes, and the router rows -- is read before any guard;
the lanes are taken with the archive's guard (or the Workshop's early locks), and the footprint is read again under
them. A job that joined a lane the deletion does not hold in between is the deletion's refusal: 409
``queue_selection_changed``, nothing written. Untouched, the deletion cancels / unlinks its jobs by compare-and-set.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def a_queue(db_session, printer_factory) -> int:
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
    await db_session.commit()
    return printer.id


async def job(item_id: int) -> PrintQueueItem:
    async with database.async_session() as db:
        return await db.get(PrintQueueItem, item_id)


def a_job_joins_between_the_footprint_and_the_guards(monkeypatch, module, name, add):
    real = getattr(module, name)
    calls = []

    async def footprint(*args, **kwargs):
        result = await real(*args, **kwargs)
        if not calls:
            calls.append(True)
            await add()
        return result

    monkeypatch.setattr(module, name, footprint)


async def test_trashing_an_archive_cancels_its_pending_job(async_client, db_session, printer_factory, archive_factory):
    queue_id = await a_queue(db_session, printer_factory)
    archive = await archive_factory(queue_id, status="completed")
    row = PrintQueueItem(queue_id=queue_id, position=1, status="pending", archive_id=archive.id)
    db_session.add(row)
    await db_session.commit()
    response = await async_client.delete(f"/api/v1/archives/{archive.id}")
    assert response.status_code == 200, response.text
    current = await job(row.id)
    assert (current.status, current.deferred_claim_token) == ("cancelled", None)


async def test_a_job_that_joined_another_lane_refuses_the_trash(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import archive_delete_scope

    queue_id = await a_queue(db_session, printer_factory)
    other = await a_queue(db_session, printer_factory)
    archive = await archive_factory(queue_id, status="completed")
    archive_id = archive.id
    first = PrintQueueItem(queue_id=queue_id, position=1, status="pending", archive_id=archive_id)
    db_session.add(first)
    await db_session.commit()
    joined: list[int] = []

    async def add():
        async with database.async_session() as db:
            late = PrintQueueItem(queue_id=other, position=1, status="pending", archive_id=archive_id)
            db.add(late)
            await db.commit()
            joined.append(late.id)

    a_job_joins_between_the_footprint_and_the_guards(monkeypatch, archive_delete_scope, "footprint", add)
    response = await async_client.delete(f"/api/v1/archives/{archive_id}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "queue_selection_changed"
    assert ((await job(first.id)).status, (await job(joined[0])).status) == ("pending", "pending")
    async with database.async_session() as db:
        assert (await db.get(PrintArchive, archive_id)).deleted_at is None, "nothing written"


async def test_a_job_that_joined_another_lane_refuses_the_order_deletion(
    committing_client, db_session, printer_factory, monkeypatch
):
    from backend.app.api.routes import projects

    created = await committing_client.post("/api/v1/projects/", json={"name": "Doomed"})
    assert created.status_code in (200, 201), created.text
    project_id = created.json()["id"]
    queue_id = await a_queue(db_session, printer_factory)
    other = await a_queue(db_session, printer_factory)
    first = PrintQueueItem(queue_id=queue_id, position=1, status="pending", project_id=project_id)
    db_session.add(first)
    await db_session.commit()
    joined: list[int] = []

    async def add():
        async with database.async_session() as db:
            late = PrintQueueItem(queue_id=other, position=1, status="pending", project_id=project_id)
            db.add(late)
            await db.commit()
            joined.append(late.id)

    a_job_joins_between_the_footprint_and_the_guards(monkeypatch, projects, "_order_link_footprint", add)
    response = await committing_client.delete(f"/api/v1/projects/{project_id}")
    assert response.status_code == 409, response.text
    assert ((await job(first.id)).project_id, (await job(joined[0])).project_id) == (project_id, project_id)
    async with database.async_session() as db:
        from backend.app.models.project import Project

        assert await db.scalar(select(Project.id).where(Project.id == project_id)) == project_id


async def test_an_untouched_order_deletion_unlinks_its_jobs(committing_client, db_session, printer_factory):
    created = await committing_client.post("/api/v1/projects/", json={"name": "Gone"})
    project_id = created.json()["id"]
    queue_id = await a_queue(db_session, printer_factory)
    row = PrintQueueItem(queue_id=queue_id, position=1, status="pending", project_id=project_id)
    db_session.add(row)
    await db_session.commit()
    response = await committing_client.delete(f"/api/v1/projects/{project_id}")
    assert response.status_code == 200, response.text
    assert (await job(row.id)).project_id is None
