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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_row_referenced_only_by_a_product_survives(db_session):
    """A file's only reference can be its product link, and that has to count.

    m141 is frozen and runs at two different moments — before m162 on an
    upgrade (legacy pivot present, product tables absent) and after
    ``create_all`` on a fresh install (the reverse) — so the counter asks the
    database which filing tables exist instead of importing either era's model.
    A reference it cannot see is a survivor it will not protect: the file a
    product is built from would lose the tie-break and be swept away, taking
    the product's plate with it.
    """
    from sqlalchemy import insert

    from backend.app.models.product import Product, product_files

    keep = await _row(db_session, file_hash="dup-product", filename="keep.3mf")
    drop = await _row(db_session, file_hash="dup-product", filename="drop.3mf")
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    await db_session.execute(insert(product_files).values(product_id=product.id, library_file_id=keep.id))
    await db_session.commit()

    groups, trashed = await trash_duplicate_rows(db_session)
    await db_session.commit()

    assert (groups, trashed) == (1, 1)
    await db_session.refresh(keep)
    await db_session.refresh(drop)
    assert keep.deleted_at is None, "the row a product is built from must survive"
    assert drop.deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_row_referenced_only_by_a_product_plate_survives(db_session):
    """Same rule for the plate rows: a product's recipe points at a file, and
    that is a reference even when the pivot row was never written."""
    from backend.app.models.product import Product, ProductPlate

    keep = await _row(db_session, file_hash="dup-plate", filename="keep.3mf")
    drop = await _row(db_session, file_hash="dup-plate", filename="drop.3mf")
    product = Product(name="Bracket")
    db_session.add(product)
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product.id, library_file_id=keep.id, plate_index=0))
    await db_session.commit()

    await trash_duplicate_rows(db_session)
    await db_session.commit()

    await db_session.refresh(keep)
    await db_session.refresh(drop)
    assert keep.deleted_at is None
    assert drop.deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_legacy_pivot_still_counts_when_it_is_there(db_session):
    """The upgrade half of the same rule, and the only place it can be tested.

    On an existing install m141 runs BEFORE m162: ``library_file_projects``
    exists, ``product_files`` does not, and the legacy table has no model left
    to select from — so it is counted in raw SQL, guarded by a table check. A
    fresh test database never has that table, which means a typo in that SQL
    would surface for the first time on somebody's upgrade. Here the table is
    made by hand so the branch actually runs.
    """
    from sqlalchemy import text

    keep = await _row(db_session, file_hash="dup-legacy", filename="keep.3mf")
    drop = await _row(db_session, file_hash="dup-legacy", filename="drop.3mf")
    await db_session.execute(
        text("CREATE TABLE library_file_projects (file_id INTEGER NOT NULL, project_id INTEGER NOT NULL)")
    )
    await db_session.execute(
        text("INSERT INTO library_file_projects (file_id, project_id) VALUES (:fid, 1)"), {"fid": keep.id}
    )
    await db_session.commit()

    try:
        await trash_duplicate_rows(db_session)
        await db_session.commit()

        await db_session.refresh(keep)
        await db_session.refresh(drop)
        assert keep.deleted_at is None, "the legacy pivot is a reference too"
        assert drop.deleted_at is not None
    finally:
        await db_session.execute(text("DROP TABLE library_file_projects"))
        await db_session.commit()
