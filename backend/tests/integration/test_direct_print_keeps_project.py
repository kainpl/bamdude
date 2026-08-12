"""Printing a project-linked file straight to a printer keeps its project.

Reported against a direct print to four printers: every archive row came back
with ``project_id`` NULL. Queueing the same file worked, because the queue and
auto-queue routes inherit the project from the library file when the caller
does not name one — and the direct-print route never did.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture
async def linked_file(db_session, tmp_path):
    """A library file that belongs to a project, as the report describes.

    The bytes really exist: the route refuses to dispatch a row whose file is
    missing from disk, which is a different failure from the one under test.
    """
    from backend.app.models.library import LibraryFile
    from backend.app.models.project import Project

    on_disk = tmp_path / "shade.gcode.3mf"
    on_disk.write_bytes(b"sliced")

    project = Project(name="Lamp")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    lib_file = LibraryFile(
        filename="shade.gcode.3mf",
        file_path=str(on_disk),
        file_size=2048,
        file_type="gcode.3mf",
    )
    lib_file.projects.append(project)
    db_session.add(lib_file)
    await db_session.commit()
    await db_session.refresh(lib_file)
    return lib_file, project


async def _print(async_client: AsyncClient, printer, file_id: int, body: dict | None = None):
    """Fire the direct-print route with the dispatcher stubbed.

    The dispatcher uploads over FTP and talks MQTT; what this test is about is
    the project it is handed, so it is captured rather than performed.
    """
    with (
        # The route refuses an offline printer, which is a different failure
        # from the one under test.
        patch(
            "backend.app.services.printer_manager.printer_manager.is_connected",
            return_value=True,
        ),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
            new=AsyncMock(return_value={"status": "dispatched", "dispatch_job_id": 1, "dispatch_position": 1}),
        ) as dispatch,
    ):
        response = await async_client.post(
            f"/api/v1/library/files/{file_id}/print?printer_id={printer.id}",
            json=body or {},
        )
    return response, dispatch


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_direct_print_inherits_the_files_project(async_client: AsyncClient, linked_file, printer_factory):
    lib_file, project = linked_file
    printer = await printer_factory()

    response, dispatch = await _print(async_client, printer, lib_file.id)

    assert response.status_code == 200, response.text
    assert dispatch.await_args is not None, response.text
    assert dispatch.await_args.kwargs["project_id"] == project.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_explicit_project_still_wins(async_client: AsyncClient, linked_file, printer_factory, db_session):
    """Naming a project is the point of the parameter; inheritance must not
    override it."""
    from backend.app.models.project import Project

    lib_file, _ = linked_file
    other = Project(name="Bracket")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    printer = await printer_factory()

    _, dispatch = await _print(async_client, printer, lib_file.id, {"project_id": other.id})

    assert dispatch.await_args.kwargs["project_id"] == other.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unlinked_file_carries_no_project(async_client: AsyncClient, printer_factory, db_session, tmp_path):
    from backend.app.models.library import LibraryFile

    on_disk = tmp_path / "loose.gcode.3mf"
    on_disk.write_bytes(b"sliced")
    lib_file = LibraryFile(filename="loose.gcode.3mf", file_path=str(on_disk), file_size=1024, file_type="gcode.3mf")
    db_session.add(lib_file)
    await db_session.commit()
    await db_session.refresh(lib_file)
    printer = await printer_factory()

    _, dispatch = await _print(async_client, printer, lib_file.id)

    assert dispatch.await_args.kwargs["project_id"] is None
