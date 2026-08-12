"""The project timeline shows every print, whatever route it took.

It used to take "print started" from ``PrintQueueItem`` and turn archives into
events only for ``completed`` / ``failed``. A print dispatched straight to a
printer therefore appeared nowhere at all — no queue row to read from, and a
``printing`` archive the endpoint ignored — which is exactly how it was found:
a machine visibly running a project's job, and an empty project history.
Cancelled prints were invisible on every route.
"""

import pytest
from httpx import AsyncClient

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project

_HASH = iter(f"timeline_hash_{i:04d}" for i in range(1000))


async def _project(db_session, name="Timeline Project") -> Project:
    project = Project(name=name)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _archive(db_session, project_id: int, status: str, **over) -> PrintArchive:
    archive = PrintArchive(
        project_id=project_id,
        filename=f"{status}.3mf",
        print_name=over.pop("print_name", f"{status} print"),
        file_path=f"/tmp/{status}.3mf",
        file_size=1024,
        content_hash=next(_HASH),
        status=status,
        **over,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _events(async_client: AsyncClient, project_id: int) -> list[dict]:
    response = await async_client.get(f"/api/v1/projects/{project_id}/timeline")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_running_without_a_queue_is_visible(async_client: AsyncClient, db_session):
    """The reported symptom. A direct / printer-started print has no queue row."""
    project = await _project(db_session)
    await _archive(db_session, project.id, "printing", print_name="Running now")

    events = await _events(async_client, project.id)

    started = [e for e in events if e["event_type"] == "print_started"]
    assert len(started) == 1
    assert started[0]["description"] == "Running now"
    assert started[0]["metadata"]["status"] == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancelled_prints_are_visible(async_client: AsyncClient, db_session):
    project = await _project(db_session)
    for status in ("cancelled", "aborted", "stopped"):
        await _archive(db_session, project.id, status)

    events = await _events(async_client, project.id)

    assert len([e for e in events if e["event_type"] == "print_cancelled"]) == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_completed_and_failed_still_appear(async_client: AsyncClient, db_session):
    project = await _project(db_session)
    await _archive(db_session, project.id, "completed", print_time_seconds=3600, filament_used_grams=12.34)
    await _archive(db_session, project.id, "failed", failure_reason="spaghetti")

    events = await _events(async_client, project.id)
    by_type = {e["event_type"]: e for e in events}

    assert by_type["print_completed"]["metadata"]["print_time_hours"] == 1.0
    assert by_type["print_completed"]["metadata"]["filament_grams"] == 12.3
    assert by_type["print_failed"]["metadata"]["failure_reason"] == "spaghetti"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_trashed_archive_is_not_an_event(async_client: AsyncClient, db_session):
    from datetime import datetime, timezone

    project = await _project(db_session)
    await _archive(db_session, project.id, "completed", deleted_at=datetime.now(timezone.utc))

    events = await _events(async_client, project.id)

    assert [e for e in events if e["event_type"].startswith("print_")] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_waiting_work_from_both_queues_is_listed(async_client: AsyncClient, db_session):
    project = await _project(db_session)
    library_file = LibraryFile(
        filename="waiting.gcode.3mf",
        file_path="library/files/waiting.3mf",
        file_type="gcode",
        file_size=2048,
        file_hash="timeline_libfile_0001",
    )
    queue = PrinterQueue(id=1, printer_id=1)
    db_session.add_all([library_file, queue])
    await db_session.commit()
    await db_session.refresh(library_file)

    db_session.add_all(
        [
            PrintQueueItem(queue_id=queue.id, project_id=project.id, library_file_id=library_file.id, status="pending"),
            AutoQueueItem(project_id=project.id, library_file_id=library_file.id, status="pending"),
        ]
    )
    await db_session.commit()

    events = await _events(async_client, project.id)
    types = [e["event_type"] for e in events]

    assert types.count("queued") == 1
    assert types.count("auto_queued") == 1
    assert all(e["description"] == "waiting.gcode.3mf" for e in events if e["event_type"].endswith("queued"))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dispatched_work_is_not_also_listed_as_waiting(async_client: AsyncClient, db_session):
    """A job that reached a printer must appear once, as the print it became.

    Both halves matter: the per-printer item has left 'pending' by then, and the
    auto-queue row that produced it is 'assigned'.
    """
    project = await _project(db_session)
    archive = await _archive(db_session, project.id, "printing", print_name="Dispatched")
    queue = PrinterQueue(id=2, printer_id=2)
    db_session.add(queue)
    await db_session.commit()

    item = PrintQueueItem(queue_id=queue.id, project_id=project.id, archive_id=archive.id, status="printing")
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)

    db_session.add(
        AutoQueueItem(project_id=project.id, archive_id=archive.id, status="assigned", assigned_to_item_id=item.id)
    )
    await db_session.commit()

    events = await _events(async_client, project.id)
    types = [e["event_type"] for e in events]

    assert types.count("print_started") == 1
    assert "queued" not in types
    assert "auto_queued" not in types


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_pending_reprint_of_a_listed_archive_is_not_duplicated(async_client: AsyncClient, db_session):
    """The overlap status alone cannot catch: a queued reprint points back at the
    archive it came from, which is already in the list as its own print."""
    project = await _project(db_session)
    archive = await _archive(db_session, project.id, "completed", print_name="Reprint me")
    queue = PrinterQueue(id=3, printer_id=3)
    db_session.add(queue)
    await db_session.commit()

    db_session.add(PrintQueueItem(queue_id=queue.id, project_id=project.id, archive_id=archive.id, status="pending"))
    await db_session.commit()

    events = await _events(async_client, project.id)
    types = [e["event_type"] for e in events]

    assert types.count("print_completed") == 1
    assert "queued" not in types


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_limit_is_spent_on_visible_events(async_client: AsyncClient, db_session):
    """Statuses are filtered in SQL. Filtering after the fetch meant a project
    whose newest rows were all uninteresting reported an empty history."""
    project = await _project(db_session)
    for _ in range(3):
        await _archive(db_session, project.id, "archived")  # legacy status, not an event
    await _archive(db_session, project.id, "completed", print_name="The real one")

    response = await async_client.get(f"/api/v1/projects/{project.id}/timeline?limit=2")
    assert response.status_code == 200, response.text
    events = response.json()

    assert len(events) == 2
    assert events[0]["event_type"] == "print_completed"
    assert events[0]["description"] == "The real one"
