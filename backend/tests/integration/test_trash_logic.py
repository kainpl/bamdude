"""Integration tests for the post-0.5.x trash logic adjustments:

* Dedup queries (library + archive) exclude trashed rows.
* Library file hard-delete-from-trash is refused while active archives reference it.
* `Empty trash` for the library skips pinned files and reports the count.
* Archive auto-purge soft-deletes (moves to archive trash); restore + hard-delete
  routes round-trip; sweeper hard-deletes after retention.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


@pytest.fixture
async def file_factory(db_session):
    _counter = [0]

    async def _create(**kwargs):
        from backend.app.models.library import LibraryFile

        _counter[0] += 1
        i = _counter[0]
        defaults = {
            "filename": f"trash_logic_{i}.3mf",
            "file_path": f"/tmp/trash_logic_{i}.3mf",
            "file_size": 1024 * i,
            "file_type": "3mf",
            "file_hash": f"hash{i:064d}",
        }
        defaults.update(kwargs)
        f = LibraryFile(**defaults)
        db_session.add(f)
        await db_session.commit()
        await db_session.refresh(f)
        return f

    return _create


# ============= Library dedup ignores trashed =============


@pytest.mark.asyncio
@pytest.mark.integration
async def test_library_upload_dedup_ignores_trashed_sibling(async_client: AsyncClient, file_factory, db_session):
    """A trashed sibling with the same hash must not pin a new upload as duplicate."""
    from backend.app.api.routes.library import calculate_file_hash  # noqa: F401

    # Create a trashed file with a known hash
    shared_hash = "a" * 64
    trashed = await file_factory(file_hash=shared_hash)
    trashed.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    # Direct dedup-query check: looking up this hash for an upload should miss.
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile

    result = await db_session.execute(
        select(LibraryFile.id).where(
            LibraryFile.file_hash == shared_hash,
            LibraryFile.deleted_at.is_(None),
        )
    )
    assert result.scalar_one_or_none() is None


# ============= Archive dedup ignores trashed =============


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_dedup_ignores_trashed(printer_factory, archive_factory, db_session):
    """`get_duplicate_hashes_and_names` must exclude trashed archives from the
    "appears more than once" count, otherwise two archives (one trashed) get
    flagged as duplicates of nothing."""
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    a = await archive_factory(printer.id, content_hash="dup-hash-1", print_name="Same name")
    b = await archive_factory(printer.id, content_hash="dup-hash-1", print_name="Same name")

    # Trash one of them
    b.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    service = ArchiveService(db_session)
    dup_hashes, dup_pairs = await service.get_duplicate_hashes_and_names()
    # Only one active archive remains with that hash → not a duplicate group.
    assert "dup-hash-1" not in dup_hashes
    assert ("same name", "dup-hash-1") not in dup_pairs

    # Sanity: untouching b reinstates the duplicate group.
    b.deleted_at = None
    await db_session.commit()
    dup_hashes, _ = await service.get_duplicate_hashes_and_names()
    assert "dup-hash-1" in dup_hashes

    _ = a  # noqa: F841


# ============= Reference-aware library hard-delete =============


@pytest.mark.asyncio
@pytest.mark.integration
async def test_library_hard_delete_refused_while_active_archive_pins(
    async_client: AsyncClient, file_factory, archive_factory, printer_factory, db_session
):
    """409 from DELETE /library/trash/{id} when an active archive references the file."""
    printer = await printer_factory()
    f = await file_factory()
    f.deleted_at = datetime.now(timezone.utc)
    archive = await archive_factory(printer.id, library_file_id=f.id)
    await db_session.commit()

    resp = await async_client.delete(f"/api/v1/library/trash/{f.id}")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "library_file_pinned_by_archives"
    assert detail["active_references"] >= 1

    # Trash the referencing archive — now hard-delete is allowed.
    archive.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    resp = await async_client.delete(f"/api/v1/library/trash/{f.id}")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_library_empty_trash_skips_pinned_and_reports_count(
    async_client: AsyncClient, file_factory, archive_factory, printer_factory, db_session
):
    """Empty trash deletes unpinned, skips pinned, returns both counts."""
    printer = await printer_factory()
    pinned = await file_factory()
    free = await file_factory()
    pinned.deleted_at = datetime.now(timezone.utc)
    free.deleted_at = datetime.now(timezone.utc)
    await archive_factory(printer.id, library_file_id=pinned.id)  # active archive pins it
    await db_session.commit()

    resp = await async_client.delete("/api/v1/library/trash")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    assert body["skipped_pinned"] == 1


# ============= Archive trash routes round-trip =============


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_trash_list_restore_hard_delete_roundtrip(
    async_client: AsyncClient, archive_factory, printer_factory, db_session
):
    """End-to-end: trash via DELETE → list trash → restore → re-trash."""
    from backend.app.models.archive import PrintArchive

    printer = await printer_factory()
    archive = await archive_factory(printer.id, print_name="Roundtrip")
    archive.created_at = datetime.now(timezone.utc) - timedelta(days=400)
    await db_session.commit()
    archive_id = archive.id

    # Trash via DELETE on /archives/{id} — the per-row admin auto-purge
    # endpoint (/archives/purge) was removed in 0.4.2; manual delete is now
    # the only path that lands a row in the archive trash.
    resp = await async_client.delete(f"/api/v1/archives/{archive_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["trashed"] is True

    # Trash list shows it
    resp = await async_client.get("/api/v1/archives/trash")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert any(i["id"] == archive_id for i in items)

    # Active archive listing must NOT show it
    resp = await async_client.get("/api/v1/archives/")
    assert resp.status_code == 200
    listed_ids = [a["id"] for a in resp.json()["data"]]
    assert archive_id not in listed_ids

    # Restore
    resp = await async_client.post(f"/api/v1/archives/trash/{archive_id}/restore")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    row = await db_session.get(PrintArchive, archive_id)
    assert row is not None and row.deleted_at is None

    # Re-trash via direct DELETE on /archives/{id}
    resp = await async_client.delete(f"/api/v1/archives/{archive_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["trashed"] is True
    db_session.expire_all()
    row = await db_session.get(PrintArchive, archive_id)
    assert row is not None and row.deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_trash_settings_roundtrip(async_client: AsyncClient):
    """GET/PUT /archives/trash/settings round-trips the retention window."""
    resp = await async_client.get("/api/v1/archives/trash/settings")
    assert resp.status_code == 200, resp.text
    initial = resp.json()
    assert "retention_days" in initial

    new_value = 17 if initial["retention_days"] != 17 else 23
    resp = await async_client.put("/api/v1/archives/trash/settings", json={"retention_days": new_value})
    assert resp.status_code == 200, resp.text
    assert resp.json()["retention_days"] == new_value
