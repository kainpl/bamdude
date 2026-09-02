"""``project_line_id`` rides every path from the request to the archive row.

An order line is the unit of work an operator actually schedules ("30 of the
lamp shade for order 1"), so every door that starts a print has to carry it:
the per-printer queue, the direct-print route, and the archive the dispatcher
writes at the end. A path that drops it silently produces a print nobody can
attribute to a line, and the order's progress stalls at a number that never
moves.

Successor of ``test_direct_print_keeps_project.py``: files no longer belong to
projects, so there is nothing left to inherit — an order is named explicitly or
not at all, and what is named must survive the trip.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import Product
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine

pytestmark = pytest.mark.integration


@pytest.fixture
async def order_line(db_session):
    """A product, an order, and one line of that order."""
    product = Product(name="Lamp")
    project = Project(name="Order 1")
    db_session.add_all([product, project])
    await db_session.flush()
    line = ProjectLine(project_id=project.id, product_id=product.id, quantity=3)
    db_session.add(line)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(line)
    return project, line


@pytest.fixture
async def linked_file(db_session, tmp_path):
    """A plain library file whose bytes really exist on disk.

    The direct-print route refuses a row whose file is missing, which is a
    different failure from the one under test.
    """
    from backend.app.models.library import LibraryFile

    on_disk = tmp_path / "shade.gcode.3mf"
    on_disk.write_bytes(b"sliced")

    lib_file = LibraryFile(
        filename="shade.gcode.3mf",
        file_path=str(on_disk),
        file_size=2048,
        file_type="gcode.3mf",
    )
    db_session.add(lib_file)
    await db_session.commit()
    await db_session.refresh(lib_file)
    return lib_file


@pytest.fixture
async def printer_with_queue(db_session, printer_factory):
    """A printer plus its ``PrinterQueue`` row, exposing ``queue_id``."""
    from backend.app.models.printer_queue import PrinterQueue

    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    printer.queue_id = queue.id
    return printer


@pytest.mark.asyncio
async def test_queue_item_carries_the_line(async_client, db_session, order_line, printer_with_queue, linked_file):
    project, line = order_line
    r = await async_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": printer_with_queue.queue_id,
            "library_file_id": linked_file.id,
            "project_id": project.id,
            "project_line_id": line.id,
        },
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["project_line_id"] == line.id
    item = (
        await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.project_id == project.id))
    ).scalar_one()
    assert item.project_line_id == line.id


@pytest.mark.asyncio
async def test_a_line_from_another_order_is_refused(
    async_client, db_session, order_line, printer_with_queue, linked_file
):
    """The line has to be a line OF the named order, or the queue row would
    claim progress against work nobody ordered."""
    _, line = order_line
    other = Project(name="Order 2")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    r = await async_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": printer_with_queue.queue_id,
            "library_file_id": linked_file.id,
            "project_id": other.id,
            "project_line_id": line.id,
        },
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_a_line_alone_names_its_own_order(async_client, db_session, order_line, printer_with_queue, linked_file):
    """Naming only the line is enough — the order it belongs to is derived."""
    project, line = order_line
    r = await async_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": printer_with_queue.queue_id,
            "library_file_id": linked_file.id,
            "project_line_id": line.id,
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["project_line_id"] == line.id
    assert body["project_id"] == project.id


@pytest.mark.asyncio
async def test_archive_print_stamps_the_line(db_session, order_line, linked_file, tmp_path):
    from backend.app.services.archive import ArchiveService

    project, line = order_line
    source = tmp_path / "shade.gcode.3mf"
    source.write_bytes(b"sliced")
    archive = await ArchiveService(db_session).archive_print(
        printer_id=None,
        source_file=source,
        project_id=project.id,
        project_line_id=line.id,
        library_file_id=linked_file.id,
    )
    assert archive is not None and archive.project_line_id == line.id


@pytest.mark.asyncio
async def test_direct_print_passes_the_line_to_the_dispatcher(
    async_client: AsyncClient, order_line, printer_with_queue, linked_file
):
    """The dispatcher is stubbed — what this is about is the kwargs it is
    handed, not the FTP upload it would otherwise perform."""
    project, line = order_line
    with (
        patch(
            "backend.app.services.printer_manager.printer_manager.is_connected",
            return_value=True,
        ),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
            new=AsyncMock(return_value={"status": "dispatched", "dispatch_job_id": 1, "dispatch_position": 1}),
        ) as dispatch,
    ):
        r = await async_client.post(
            f"/api/v1/library/files/{linked_file.id}/print?printer_id={printer_with_queue.id}",
            json={"project_id": project.id, "project_line_id": line.id},
        )
    assert r.status_code == 200, r.text
    assert dispatch.await_args is not None, r.text
    assert dispatch.await_args.kwargs["project_id"] == project.id
    assert dispatch.await_args.kwargs["project_line_id"] == line.id


@pytest.mark.asyncio
async def test_a_direct_print_without_an_order_carries_no_line(
    async_client: AsyncClient, printer_with_queue, linked_file
):
    """Nothing is inherited from the file any more — an unnamed order stays
    unnamed all the way down."""
    with (
        patch(
            "backend.app.services.printer_manager.printer_manager.is_connected",
            return_value=True,
        ),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
            new=AsyncMock(return_value={"status": "dispatched", "dispatch_job_id": 1, "dispatch_position": 1}),
        ) as dispatch,
    ):
        r = await async_client.post(
            f"/api/v1/library/files/{linked_file.id}/print?printer_id={printer_with_queue.id}",
            json={},
        )
    assert r.status_code == 200, r.text
    assert dispatch.await_args.kwargs["project_id"] is None
    assert dispatch.await_args.kwargs["project_line_id"] is None


@pytest.mark.asyncio
async def test_auto_queue_item_carries_the_line(async_client, db_session, order_line, linked_file):
    """The distributor's own door. An auto-queue row is copied onto a printer
    queue row by the scheduler, so a line dropped here is dropped for every
    print the router places."""
    from backend.app.models.auto_queue import AutoQueueItem

    project, line = order_line
    r = await async_client.post(
        "/api/v1/auto-queue/",
        json={"library_file_id": linked_file.id, "project_id": project.id, "project_line_id": line.id},
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["project_line_id"] == line.id
    row = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.project_id == project.id))).scalar_one()
    assert row.project_line_id == line.id


@pytest.mark.asyncio
async def test_the_scheduler_copies_the_line_onto_the_printer_row(db_session, order_line, linked_file):
    """``auto_queue_scheduler`` builds the per-printer row by hand, field by
    field — the one place a new column is silently left behind."""
    import inspect

    from backend.app.services import auto_queue_scheduler

    source = inspect.getsource(auto_queue_scheduler)
    assert "project_line_id=item.project_line_id" in source
