"""The listing carries how many times a file has been printed.

``LibraryFile.print_count`` has existed and been maintained for a long time —
it was simply never included in the LIST response, only the detail one, so the
file manager had no way to show it.
"""

import pytest
from httpx import AsyncClient


async def _file(db_session, **kwargs):
    from backend.app.models.library import LibraryFile

    defaults = {
        "filename": "cube.gcode.3mf",
        "file_path": "/tmp/cube.gcode.3mf",
        "file_size": 2048,
        "file_type": "gcode",
    }
    defaults.update(kwargs)
    row = LibraryFile(**defaults)
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_listing_reports_a_files_print_count(async_client: AsyncClient, db_session):
    """The assertion that fails when the schema gains the field and the
    construction site does not: every row would come back 0."""
    await _file(db_session, filename="printed.gcode.3mf", print_count=4)

    response = await async_client.get("/api/v1/library/files")
    assert response.status_code == 200
    row = next(r for r in response.json() if r["filename"] == "printed.gcode.3mf")

    assert row["print_count"] == 4


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_file_that_was_never_printed_reports_zero(async_client: AsyncClient, db_session):
    """Zero, not null — the UI decides what to draw for it, and a null would
    make every read site handle a second empty case for no reason."""
    await _file(db_session, filename="fresh.gcode.3mf")

    response = await async_client.get("/api/v1/library/files")
    row = next(r for r in response.json() if r["filename"] == "fresh.gcode.3mf")

    assert row["print_count"] == 0
