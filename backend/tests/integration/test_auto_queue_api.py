"""Integration tests for the /auto-queue REST endpoints.

Regression coverage for the response builder. The original feature commit
(81ae73f) accessed ``item.archive.original_filename`` / ``item.library_file
.original_filename`` — neither model has that attribute. Every successful
POST therefore committed the rows, then raised ``AttributeError`` while
serialising the response, so the client got 500 even though the items had
already landed in the table. Operators retried, items duplicated, and the
auto-queue scheduler picked up more rows than the user expected.

These tests round-trip a POST → GET so the builder runs against rows with
both an ``archive`` relationship and a ``library_file`` relationship
populated. A latent ``AttributeError`` would surface as 500 on POST or as
the GET list collapsing on the first row.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_post_auto_queue_with_archive_returns_200_and_includes_archive_name(
    async_client: AsyncClient, db_session
):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        filename="multi_plate.3mf",
        print_name="Multi Plate Project",
        file_path="/tmp/multi_plate.3mf",
        file_size=1024,
        content_hash="aq_archive_hash_0001",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    response = await async_client.post("/api/v1/auto-queue/", json={"archive_id": archive.id})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["archive_id"] == archive.id
    assert payload["library_file_id"] is None
    # ``print_name`` wins over ``filename`` for archive surface.
    assert payload["archive_name"] == "Multi Plate Project"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_post_auto_queue_with_library_file_returns_200_and_includes_filename(
    async_client: AsyncClient, db_session
):
    from backend.app.models.library import LibraryFile

    library_file = LibraryFile(
        filename="part_x5.gcode.3mf",
        file_path="library/files/aq_libfile_hash_0001.3mf",
        file_type="gcode",
        file_size=2048,
        file_hash="aq_libfile_hash_0001",
    )
    db_session.add(library_file)
    await db_session.commit()
    await db_session.refresh(library_file)

    response = await async_client.post("/api/v1/auto-queue/", json={"library_file_id": library_file.id})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["library_file_id"] == library_file.id
    assert payload["archive_id"] is None
    # No ``file_metadata['print_name']`` present → falls back to ``filename``.
    assert payload["library_file_name"] == "part_x5.gcode.3mf"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_auto_queue_list_renders_with_loaded_relationships(async_client: AsyncClient, db_session):
    """GET /auto-queue/ must serialise every row's relationships without
    raising. The original AttributeError would surface on the first row
    that had a non-null archive / library_file."""
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile

    archive = PrintArchive(
        filename="aq_get_archive.3mf",
        print_name="Archive Row",
        file_path="/tmp/aq_get_archive.3mf",
        file_size=1024,
        content_hash="aq_archive_hash_0002",
        status="completed",
    )
    library_file = LibraryFile(
        filename="aq_get_libfile.gcode.3mf",
        file_path="library/files/aq_get_libfile.3mf",
        file_type="gcode",
        file_size=2048,
        file_hash="aq_libfile_hash_0002",
        file_metadata={"print_name": "Library Print Name"},
    )
    db_session.add_all([archive, library_file])
    await db_session.commit()
    await db_session.refresh(archive)
    await db_session.refresh(library_file)

    for body in (
        {"archive_id": archive.id},
        {"library_file_id": library_file.id},
    ):
        post_resp = await async_client.post("/api/v1/auto-queue/", json=body)
        assert post_resp.status_code == 200, post_resp.text

    list_resp = await async_client.get("/api/v1/auto-queue/")
    assert list_resp.status_code == 200, list_resp.text
    payload = list_resp.json()
    assert len(payload) >= 2
    archive_row = next(item for item in payload if item["archive_id"] == archive.id)
    library_row = next(item for item in payload if item["library_file_id"] == library_file.id)
    assert archive_row["archive_name"] == "Archive Row"
    # ``file_metadata['print_name']`` wins over the bare filename for library files.
    assert library_row["library_file_name"] == "Library Print Name"
