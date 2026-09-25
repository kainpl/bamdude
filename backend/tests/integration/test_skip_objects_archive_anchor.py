"""The objects endpoint reads the archive OF THE RUNNING PRINT, found by subtask_id (upstream cfecfa36).

After a restart mid-print the object list is empty until something rebuilds it;
the endpoint reads the archived 3MF from disk before asking the printer. It used
to take the newest ``status="printing"`` row of the printer — and a leftover
printing row from a completion that was never seen could lend its objects to
another job. The firmware mints a ``subtask_id`` per print: that is the anchor,
and without one the archive is not guessed at.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.core.config import settings


async def _printing_archive(db, printer_id: int, name: str, subtask_id: str | None):
    from backend.app.models.archive import PrintArchive

    rel = f"archive/{name}.3mf"
    path = settings.base_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    archive = PrintArchive(
        printer_id=printer_id,
        filename=f"{name}.3mf",
        file_path=rel,
        file_size=1,
        status="printing",
        subtask_id=subtask_id,
    )
    db.add(archive)
    await db.commit()
    return archive


def _extract(data: bytes, **_):
    """The 'parsed objects' are named after the file they came from."""
    return {1: data.decode(), 2: f"{data.decode()}-2"}, [0, 0, 1, 1], False


def _client(subtask_id: str | None):
    client = MagicMock()
    client.state.printable_objects = {}
    client.state.skipped_objects = []
    client.state.state = "RUNNING"
    client.state.subtask_name = None  # no FTP fallback in these tests
    client.state.subtask_id = subtask_id
    return client


async def _get(async_client, printer_id, client):
    with (
        patch("backend.app.api.routes.printers.printer_manager") as pm,
        patch("backend.app.services.archive.extract_printable_objects_from_3mf", side_effect=_extract),
        patch("backend.app.services.archive.extract_skip_support_from_3mf", return_value=True),
    ):
        pm.get_client.return_value = client
        pm.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
        return await async_client.get(f"/api/v1/printers/{printer_id}/print/objects")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_running_prints_archive_answers_not_a_newer_leftover(
    async_client: AsyncClient, db_session, printer_factory
):
    printer = await printer_factory()
    await _printing_archive(db_session, printer.id, "current", "222")
    await _printing_archive(db_session, printer.id, "leftover", "111")  # newer, never completed

    response = await _get(async_client, printer.id, _client("222"))

    assert response.status_code == 200, response.text
    assert {o["name"] for o in response.json()["objects"]} == {"current", "current-2"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_without_a_subtask_id_no_archive_is_guessed(async_client: AsyncClient, db_session, printer_factory):
    printer = await printer_factory()
    await _printing_archive(db_session, printer.id, "some-print", "222")

    response = await _get(async_client, printer.id, _client(None))

    assert response.status_code == 200, response.text
    assert response.json()["objects"] == []
