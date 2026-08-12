"""Bulk delete and folder delete must reach the trash, like the single file does.

There were four ways to delete a library file and only ONE of them honoured the
trash. The other three — bulk files, bulk folders, and deleting a folder
outright — unlinked the bytes and dropped the row, so a multi-select delete was
permanent while the identical single delete was reversible.
"""

import pytest
from httpx import AsyncClient


@pytest.fixture
async def file_factory(db_session):
    """LibraryFile rows whose bytes really exist, so a hard delete is visible."""
    _counter = [0]

    async def _create_file(tmp_path=None, **kwargs):
        from backend.app.models.library import LibraryFile

        _counter[0] += 1
        counter = _counter[0]
        defaults = {
            "filename": f"bulk_test_{counter}.3mf",
            "file_path": f"/tmp/bulk_test_{counter}.3mf",
            "file_size": 1024 * counter,
            "file_type": "3mf",
        }
        defaults.update(kwargs)
        lib_file = LibraryFile(**defaults)
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)
        return lib_file

    return _create_file


@pytest.fixture
async def folder_factory(db_session):
    _counter = [0]

    async def _create_folder(**kwargs):
        from backend.app.models.library import LibraryFolder

        _counter[0] += 1
        defaults = {"name": f"bulk_folder_{_counter[0]}"}
        defaults.update(kwargs)
        folder = LibraryFolder(**defaults)
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)
        return folder

    return _create_folder


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_delete_moves_files_to_trash(async_client: AsyncClient, file_factory, db_session):
    """The reported bug: selecting files and pressing delete destroyed them."""
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile

    one = await file_factory()
    two = await file_factory()

    response = await async_client.post(
        "/api/v1/library/bulk-delete", json={"file_ids": [one.id, two.id], "folder_ids": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_files"] == 2

    # Columns, not mapped objects: this session created those two rows, so its
    # identity map would hand back its own stale copies and the assertion would
    # be about the test rather than the route.
    rows = (
        await db_session.execute(
            select(LibraryFile.id, LibraryFile.deleted_at).where(LibraryFile.id.in_([one.id, two.id]))
        )
    ).all()
    assert len(rows) == 2, "the rows must survive — the trash is what makes restore possible"
    assert all(row.deleted_at is not None for row in rows)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_deleted_files_can_be_restored(async_client: AsyncClient, file_factory, db_session):
    """The point of the trash, asserted end to end rather than by column."""
    f = await file_factory()

    await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": [f.id], "folder_ids": []})

    listed = (await async_client.get("/api/v1/library/trash")).json()
    assert f.id in [row["id"] for row in listed["items"]]

    restored = await async_client.post(f"/api/v1/library/trash/{f.id}/restore")
    assert restored.status_code == 200, restored.text

    await db_session.refresh(f)
    assert f.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_delete_keeps_the_bytes(async_client: AsyncClient, file_factory, tmp_path, db_session):
    """Restore is worthless if the bytes went with the row."""
    real = tmp_path / "kept.3mf"
    real.write_bytes(b"3mf bytes")
    f = await file_factory(file_path=str(real))

    await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": [f.id], "folder_ids": []})

    assert real.exists(), "a trashed file's bytes stay on disk until the sweeper takes them"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_external_file_is_still_purged(async_client: AsyncClient, file_factory, db_session):
    """External bytes live outside BamDude, so there is nothing to restore —
    the single-file path has always treated them this way."""
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile

    f = await file_factory(is_external=True)

    await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": [f.id], "folder_ids": []})

    remaining = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == f.id))).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_file_being_printed_is_reported_as_skipped(
    async_client: AsyncClient, file_factory, printer_factory, db_session
):
    """It was skipped and counted as deleted: the response carried no skip
    count at all, so the interface said "2 files deleted" for one."""
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    printer = await printer_factory()
    busy = await file_factory()
    free = await file_factory()

    # A queue item hangs off a PrinterQueue, not off a printer: printer_id is a
    # read-only convenience property that reads through the relationship.
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    db_session.add(PrintQueueItem(queue_id=queue.id, library_file_id=busy.id, status="printing"))
    await db_session.commit()

    body = (
        await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": [busy.id, free.id], "folder_ids": []})
    ).json()

    assert body["deleted_files"] == 1
    assert body["skipped_files"] == 1

    still_there = (
        await db_session.execute(select(LibraryFile.deleted_at).where(LibraryFile.id == busy.id))
    ).scalar_one()
    assert still_there is None, "a file mid-print is not deleted at all"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_a_folder_puts_its_files_in_the_trash(
    async_client: AsyncClient, file_factory, folder_factory, db_session
):
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile

    folder = await folder_factory()
    inside = await file_factory(folder_id=folder.id)

    response = await async_client.delete(f"/api/v1/library/folders/{folder.id}")
    assert response.status_code == 200, response.text

    row = (
        await db_session.execute(
            select(LibraryFile.deleted_at, LibraryFile.folder_id).where(LibraryFile.id == inside.id)
        )
    ).first()
    assert row is not None, "the folder's CASCADE must not take the trashed rows with it"
    assert row.deleted_at is not None
    assert row.folder_id is None, "detached, or the cascade deletes it the moment the folder goes"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_deleting_a_folder_puts_its_files_in_the_trash(
    async_client: AsyncClient, file_factory, folder_factory, db_session
):
    """Same action, the other door."""
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile

    folder = await folder_factory()
    inside = await file_factory(folder_id=folder.id)

    response = await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": [], "folder_ids": [folder.id]})
    assert response.status_code == 200, response.text

    row = (
        await db_session.execute(
            select(LibraryFile.deleted_at, LibraryFile.folder_id).where(LibraryFile.id == inside.id)
        )
    ).first()
    assert row is not None
    assert row.deleted_at is not None
    assert row.folder_id is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_file_already_in_the_trash_is_not_counted_twice(async_client: AsyncClient, file_factory, db_session):
    """The bulk path looked the file up without the active() filter, so a row
    already in the trash was processed again."""
    f = await file_factory()
    await async_client.delete(f"/api/v1/library/files/{f.id}")

    body = (await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": [f.id], "folder_ids": []})).json()

    assert body["deleted_files"] == 0
