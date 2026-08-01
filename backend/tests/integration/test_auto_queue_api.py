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
from sqlalchemy import select


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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancelling_an_unrouted_item_deletes_the_row(async_client: AsyncClient, db_session):
    """Cancel before dispatch is a hard delete, not ``status='cancelled'``.

    A row that never reached a printer has no ``print_queue`` partner, and
    ``queue_counters.detach_print_queue_refs`` — the only delete this table has
    — starts from exactly that partner. Parked as ``cancelled`` the row was
    therefore unreachable by every reader (the scheduler selects
    ``status='pending'``, the UI asks for ``?status=pending``, the counters come
    off ``print_archives.from_auto_queue``) *and* undeletable, accumulating one
    per cancel.

    Also covers the response: it is built while the row still exists, and it
    reads the relationships, which an ``AsyncSession`` cannot lazy-load — so a
    plain ``select()`` here would 500 after having already committed.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.models.auto_queue import AutoQueueItem

    archive = PrintArchive(
        filename="aq_cancel.3mf",
        print_name="Cancel Me",
        file_path="/tmp/aq_cancel.3mf",
        file_size=1024,
        content_hash="aq_archive_hash_0003",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    created = await async_client.post("/api/v1/auto-queue/", json={"archive_id": archive.id})
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    response = await async_client.delete(f"/api/v1/auto-queue/{item_id}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "cancelled"
    assert payload["cancelled_at"] is not None
    assert payload["archive_name"] == "Cancel Me"

    row = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id == item_id))).scalar_one_or_none()
    assert row is None, "an un-routed cancel must delete the row, not park it as cancelled"

    # The row is gone, so a repeat cancel is a 404 rather than a ghost 200.
    repeat = await async_client.delete(f"/api/v1/auto-queue/{item_id}")
    assert repeat.status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancelling_a_batch_deletes_its_unrouted_rows(async_client: AsyncClient, db_session):
    """Batch cancel follows the same rule as the single-item cancel."""
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.library import LibraryFile

    library_file = LibraryFile(
        filename="aq_batch_cancel.gcode.3mf",
        file_path="library/files/aq_batch_cancel.3mf",
        file_type="gcode",
        file_size=2048,
        file_hash="aq_libfile_hash_0003",
    )
    db_session.add(library_file)
    await db_session.commit()
    await db_session.refresh(library_file)

    created = await async_client.post("/api/v1/auto-queue/", json={"library_file_id": library_file.id, "quantity": 3})
    assert created.status_code == 200, created.text
    batch_id = created.json()["batch_id"]
    assert batch_id

    response = await async_client.delete(f"/api/v1/auto-queue/batch/{batch_id}")
    assert response.status_code == 200, response.text
    assert response.json()["affected"] == 3

    rows = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.batch_id == batch_id))).scalars().all()
    assert list(rows) == [], "batch cancel must delete un-routed rows"
