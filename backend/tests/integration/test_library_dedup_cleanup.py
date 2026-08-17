"""Duplicates already in the library go to the trash — nothing is re-pointed.

Exercises ``trash_duplicate_rows`` directly, because that is how it is reached:
m141 calls it once per install, at the upgrade that introduced deduplication.
There is no endpoint and no button — the cleanup is a once-in-a-lifetime job, and
permanent UI for it would be clutter that only helps operators who knew to look.

⚠️ Soft-delete is what makes it safe to do unasked. A merge would have to
reconcile four uniqueness constraints — makerworld meta is 1:1 per file; tags,
projects and plan items are unique pairs, and a plan item carries a copy count
and an order, so merging means summing or choosing. And a hash duplicate is not
always a duplicate to the person who filed it: two MakerWorld profiles can
produce byte-identical 3MFs.

Setting ``deleted_at`` leaves every foreign key intact and is reversible.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services.library_ingest import trash_duplicate_rows


async def _row(db, *, file_hash: str, filename: str) -> LibraryFile:
    row = LibraryFile(
        filename=filename,
        file_path=f"files/{filename}",
        file_type="3mf",
        file_size=10,
        file_hash=file_hash,
    )
    db.add(row)
    await db.flush()
    return row


async def _attach_archive(db, row: LibraryFile) -> PrintArchive:
    archive = PrintArchive(
        printer_id=None,
        file_path="",
        file_size=0,
        print_name=f"print of {row.filename}",
        filename=row.filename,
        status="completed",
        library_file_id=row.id,
    )
    db.add(archive)
    await db.flush()
    return archive


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_referenced_row_survives(db_session):
    keep = await _row(db_session, file_hash="dup-a", filename="keep.3mf")
    drop = await _row(db_session, file_hash="dup-a", filename="drop.3mf")
    await _attach_archive(db_session, keep)
    await db_session.commit()

    groups, trashed = await trash_duplicate_rows(db_session)
    await db_session.commit()

    assert (groups, trashed) == (1, 1)
    await db_session.refresh(keep)
    await db_session.refresh(drop)
    assert keep.deleted_at is None, "the row a print history points at must survive"
    assert drop.deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_when_all_are_referenced_the_lowest_id_survives(db_session):
    """Oldest wins. The sweeper eventually purges what nobody rescued, so the
    tie-break decides which row's attachments outlive the retention window."""
    first = await _row(db_session, file_hash="dup-b", filename="first.3mf")
    second = await _row(db_session, file_hash="dup-b", filename="second.3mf")
    await _attach_archive(db_session, first)
    await _attach_archive(db_session, second)
    await db_session.commit()

    await trash_duplicate_rows(db_session)
    await db_session.commit()

    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.deleted_at is None
    assert second.deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_trashed_rows_foreign_keys_are_left_intact(db_session):
    """The whole point: nothing is re-pointed, so nothing can collide — and the
    print history keeps its link even to a row that is now in the trash."""
    keep = await _row(db_session, file_hash="dup-c", filename="keep.3mf")
    drop = await _row(db_session, file_hash="dup-c", filename="drop.3mf")
    await _attach_archive(db_session, keep)
    archive_on_loser = await _attach_archive(db_session, drop)
    await _attach_archive(db_session, keep)
    await db_session.commit()

    await trash_duplicate_rows(db_session)
    await db_session.commit()

    await db_session.refresh(drop)
    await db_session.refresh(archive_on_loser)
    assert drop.deleted_at is not None
    assert archive_on_loser.library_file_id == drop.id, "history keeps its link"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_library_without_duplicates_is_left_alone(db_session):
    """The measured state of the farm this was built on: zero duplicate groups.
    On such an install m141 must be a no-op, not a rewrite of 134 rows."""
    only = await _row(db_session, file_hash="unique-a", filename="a.3mf")
    other = await _row(db_session, file_hash="unique-b", filename="b.3mf")
    await db_session.commit()

    assert await trash_duplicate_rows(db_session) == (0, 0)
    await db_session.commit()

    for row in (only, other):
        await db_session.refresh(row)
        assert row.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_running_it_again_trashes_nothing(db_session):
    """m141 runs once — but ``_run_pending`` re-runs the latest migration under
    DEBUG, so a second pass must see one active row per group and stop."""
    keep = await _row(db_session, file_hash="dup-d", filename="keep.3mf")
    await _row(db_session, file_hash="dup-d", filename="drop.3mf")
    await db_session.commit()

    first_run = await trash_duplicate_rows(db_session)
    await db_session.commit()
    second_run = await trash_duplicate_rows(db_session)
    await db_session.commit()

    assert first_run == (1, 1)
    assert second_run == (0, 0)
    survivors = (await db_session.execute(select(LibraryFile).where(LibraryFile.file_hash == "dup-d"))).scalars().all()
    assert [r.id for r in survivors if r.deleted_at is None] == [keep.id]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_three_copies_leave_exactly_one(db_session):
    """The loop trashes every loser in a group, not just the runner-up."""
    await _row(db_session, file_hash="dup-e", filename="a.3mf")
    await _row(db_session, file_hash="dup-e", filename="b.3mf")
    await _row(db_session, file_hash="dup-e", filename="c.3mf")
    await db_session.commit()

    assert await trash_duplicate_rows(db_session) == (1, 2)
    await db_session.commit()

    rows = (await db_session.execute(select(LibraryFile).where(LibraryFile.file_hash == "dup-e"))).scalars().all()
    assert sum(1 for r in rows if r.deleted_at is None) == 1
