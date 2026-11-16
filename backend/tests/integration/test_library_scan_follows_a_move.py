"""A file renamed or moved on a share keeps its library row (review B-M11).

Notes, photos, tags and product links belong to the ROW. The scan used to read
a rename as "one file vanished, another appeared": the old row was deleted —
with every note and photo, and no trash — and a new one filed. When the walk
finds a new path whose bytes are those of exactly one row that vanished in the
same scan of the same mount, and it is the only new path with those bytes, the
row is kept and re-addressed instead. Anything less certain is treated as it
always was.
"""

from __future__ import annotations

import io
import shutil

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_file_note import LibraryFileNote
from backend.app.models.library_file_photo import LibraryFilePhoto
from backend.app.models.library_scan import LibraryScanJob
from backend.app.services import library_file_photos as photos
from backend.app.services.library_scan import run_scan
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.usefixtures("library_worker")]

_RED = {1: [{"id": 1, "type": "PLA", "color": "#FF0000", "used_g": "1"}]}
_BLUE = {1: [{"id": 1, "type": "PETG", "color": "#0000FF", "used_g": "2"}]}


@pytest.fixture
async def mount(db_session: AsyncSession, tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    folder = LibraryFolder(name="NAS", is_external=True, external_path=str(root), external_show_hidden=False)
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return root, folder


async def _scan(db: AsyncSession, folder: LibraryFolder) -> LibraryScanJob:
    job = LibraryScanJob(folder_id=folder.id, status="queued")
    db.add(job)
    await db.commit()
    await run_scan(job.id)
    await photos.drain()
    await db.refresh(job)
    assert job.status == "finished", job.error
    return job


async def _rows(db: AsyncSession) -> list[LibraryFile]:
    stmt = select(LibraryFile).order_by(LibraryFile.id).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars())


async def _row(db: AsyncSession, filename: str) -> LibraryFile:
    stmt = select(LibraryFile).where(LibraryFile.filename == filename).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16), (200, 30, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


async def _annotate(db: AsyncSession, row: LibraryFile) -> None:
    """A note and a photo: what a rename used to destroy."""
    db.add(LibraryFileNote(library_file_id=row.id, user_id=None, body="prints best at 0.2"))
    await db.commit()
    await photos.add_photo(db, row, _jpeg(), "result.jpg", None, None)


async def _annotations(db: AsyncSession, file_id: int) -> tuple[int, int, int]:
    notes = await db.scalar(
        select(func.count()).select_from(LibraryFileNote).where(LibraryFileNote.library_file_id == file_id)
    )
    photo_rows = await db.scalar(
        select(func.count()).select_from(LibraryFilePhoto).where(LibraryFilePhoto.library_file_id == file_id)
    )
    folder = photos.photos_dir(file_id)
    return int(notes or 0), int(photo_rows or 0), len(list(folder.iterdir())) if folder.exists() else 0


async def test_a_file_renamed_on_the_share_keeps_its_row_notes_and_photos(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    before = await _row(db_session, "lamp.3mf")
    file_id, digest, tags = before.id, before.file_hash, list(before.file_tags)
    await _annotate(db_session, before)

    (root / "lamp.3mf").rename(root / "lantern.3mf")
    job = await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert [(r.id, r.filename) for r in rows] == [(file_id, "lantern.3mf")]
    moved = rows[0]
    assert moved.file_path == str(root / "lantern.3mf")
    assert moved.file_hash == digest and moved.folder_id == folder.id and list(moved.file_tags) == tags
    assert await _annotations(db_session, file_id) == (1, 1, 2)
    assert (job.files_added, job.files_removed, job.files_updated) == (0, 0, 1)


async def test_a_file_moved_into_a_subfolder_follows_it(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    await _annotate(db_session, await _row(db_session, "lamp.3mf"))

    (root / "done").mkdir()
    (root / "lamp.3mf").rename(root / "done" / "lamp.3mf")
    await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert [r.id for r in rows] == [file_id]
    sub = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "done"))).scalar_one()
    assert rows[0].folder_id == sub.id and rows[0].file_path == str(root / "done" / "lamp.3mf")
    assert await _annotations(db_session, file_id) == (1, 1, 2)


async def test_a_renamed_directory_takes_its_files_rows_along(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _RED)
    write_routing_3mf(root / "wip" / "vase.3mf", _BLUE)
    await _scan(db_session, folder)
    ids = {r.filename: r.id for r in await _rows(db_session)}
    await _annotate(db_session, await _row(db_session, "lamp.3mf"))

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert {r.filename: r.id for r in rows} == ids
    finished = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "finished"))).scalar_one()
    assert {r.folder_id for r in rows} == {finished.id}
    assert await _annotations(db_session, ids["lamp.3mf"]) == (1, 1, 2)
    gone = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "wip"))).scalar_one_or_none()
    assert gone is None, "the directory that no longer exists keeps no folder row"


async def test_a_copy_beside_the_original_is_not_a_move(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id

    shutil.copyfile(root / "lamp.3mf", root / "lamp copy.3mf")
    await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert [(r.id, r.filename, r.file_path) for r in rows] == [(file_id, "lamp.3mf", str(root / "lamp.3mf"))]


async def test_two_new_paths_with_the_vanished_file_s_bytes_are_not_guessed_between(db_session, mount):
    """Which of the two is "the" file nobody can say: as before, the row goes and one is filed anew."""
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    old = await _row(db_session, "lamp.3mf")
    file_id = old.id
    await _annotate(db_session, old)

    shutil.copyfile(root / "lamp.3mf", root / "a.3mf")
    (root / "lamp.3mf").rename(root / "b.3mf")
    job = await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert rows and {r.filename for r in rows} <= {"a.3mf", "b.3mf"}
    assert file_id not in {r.id for r in rows}, "neither new path was taken for the old row"
    assert job.files_removed == 1 and job.files_added == len(rows)


async def test_other_bytes_under_a_new_name_are_a_new_file(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    await _annotate(db_session, await _row(db_session, "lamp.3mf"))

    (root / "lamp.3mf").unlink()
    write_routing_3mf(root / "vase.3mf", _BLUE)
    job = await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert [r.filename for r in rows] == ["vase.3mf"]
    assert (job.files_added, job.files_removed) == (1, 1)


async def test_a_renamed_file_keeps_being_the_same_row_on_the_scan_after(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    (root / "lamp.3mf").rename(root / "lantern.3mf")
    await _scan(db_session, folder)

    job = await _scan(db_session, folder)

    assert [(r.id, r.filename) for r in await _rows(db_session)] == [(file_id, "lantern.3mf")]
    assert (job.files_added, job.files_removed, job.files_updated) == (0, 0, 0)


# ── what else hangs on the row: tags, product links, the type its name states ──


async def _product(db: AsyncSession, name: str) -> int:
    from backend.app.models.product import Product

    product = Product(name=name)
    db.add(product)
    await db.commit()
    return product.id


async def _products_of(db: AsyncSession, file_id: int) -> set[int]:
    from backend.app.models.product import product_files

    stmt = select(product_files.c.product_id).where(product_files.c.library_file_id == file_id)
    return set((await db.execute(stmt)).scalars())


async def _plates_of(db: AsyncSession, file_id: int) -> set[int]:
    from backend.app.models.product import ProductPlate

    stmt = select(ProductPlate.product_id).where(ProductPlate.library_file_id == file_id)
    return set((await db.execute(stmt)).scalars())


async def _link(db: AsyncSession, file_id: int, product_id: int) -> None:
    from backend.app.services.product_sync import sync_product_for_file

    await sync_product_for_file(db, library_file_id=file_id, product_ids=[product_id])
    await db.commit()


async def _user_tag(db: AsyncSession, file_id: int) -> int:
    from backend.app.models.library import LibraryFileTag, LibraryTag

    tag = LibraryTag(name="kid-safe", name_key="kid-safe")
    db.add(tag)
    await db.flush()
    db.add(LibraryFileTag(file_id=file_id, tag_id=tag.id))
    await db.commit()
    return tag.id


async def _tags_of(db: AsyncSession, file_id: int) -> set[int]:
    from backend.app.models.library import LibraryFileTag

    return set((await db.execute(select(LibraryFileTag.tag_id).where(LibraryFileTag.file_id == file_id))).scalars())


async def test_a_renamed_file_keeps_its_tags_and_product_links(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    product = await _product(db_session, "Lamp")
    await _link(db_session, file_id, product)
    tag = await _user_tag(db_session, file_id)
    assert await _plates_of(db_session, file_id) == {product}

    (root / "lamp.3mf").rename(root / "lantern.3mf")
    await _scan(db_session, folder)

    assert [r.id for r in await _rows(db_session)] == [file_id]
    assert await _products_of(db_session, file_id) == {product}
    assert await _plates_of(db_session, file_id) == {product}
    assert tag in await _tags_of(db_session, file_id)


async def test_a_renamed_directory_keeps_its_files_product_links_and_tags(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    product = await _product(db_session, "Lamp")
    await _link(db_session, file_id, product)
    tag = await _user_tag(db_session, file_id)

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    assert [r.id for r in await _rows(db_session)] == [file_id]
    assert await _products_of(db_session, file_id) == {product}
    assert await _plates_of(db_session, file_id) == {product}
    assert tag in await _tags_of(db_session, file_id)


async def test_a_file_moved_into_a_linked_folder_joins_its_products_and_keeps_its_own(db_session, mount):
    """The folder's link is a standing rule for whatever lands in it; the file's own links are not withdrawn."""
    from backend.app.services.product_sync import apply_folder_products

    root, folder = mount
    (root / "kits").mkdir()
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    own, of_folder = await _product(db_session, "Lamp"), await _product(db_session, "Kit")
    await _link(db_session, file_id, own)
    kits = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "kits"))).scalar_one()
    await apply_folder_products(db_session, folder_id=kits.id, product_ids=[of_folder])
    await db_session.commit()

    (root / "lamp.3mf").rename(root / "kits" / "lamp.3mf")
    await _scan(db_session, folder)

    assert [r.id for r in await _rows(db_session)] == [file_id]
    assert await _products_of(db_session, file_id) == {own, of_folder}
    assert await _plates_of(db_session, file_id) == {own, of_folder}


async def test_a_file_moved_out_of_a_linked_folder_keeps_the_links_it_had(db_session, mount):
    from backend.app.services.product_sync import apply_folder_products

    root, folder = mount
    (root / "kits").mkdir()
    await _scan(db_session, folder)
    kit = await _product(db_session, "Kit")
    kits = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "kits"))).scalar_one()
    await apply_folder_products(db_session, folder_id=kits.id, product_ids=[kit])
    await db_session.commit()
    write_routing_3mf(root / "kits" / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    assert await _products_of(db_session, file_id) == {kit}

    (root / "kits" / "lamp.3mf").rename(root / "lamp.3mf")
    await _scan(db_session, folder)

    assert [r.id for r in await _rows(db_session)] == [file_id]
    assert await _products_of(db_session, file_id) == {kit}


async def test_a_rename_that_changes_the_extension_follows_the_new_name(db_session, mount):
    """What the NAME says (the type, whether the file is read as a 3MF at all) is the new name's."""
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    before = await _row(db_session, "lamp.3mf")
    file_id, digest = before.id, before.file_hash
    assert before.file_type == "3mf" and (before.file_metadata or {}).get("plates")
    await _annotate(db_session, before)

    (root / "lamp.3mf").rename(root / "lamp.stl")
    await _scan(db_session, folder)

    rows = await _rows(db_session)
    assert [(r.id, r.filename, r.file_type, r.file_hash) for r in rows] == [(file_id, "lamp.stl", "stl", digest)]
    assert not (rows[0].file_metadata or {}).get("plates")
    assert await _annotations(db_session, file_id) == (1, 1, 2)


async def test_open_tabs_are_told_about_the_moved_row(db_session, mount, monkeypatch):
    from backend.app.core.websocket import ws_manager

    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _RED)
    await _scan(db_session, folder)
    file_id = (await _row(db_session, "lamp.3mf")).id
    told: list[dict] = []

    async def record(payload):
        told.append(payload)

    monkeypatch.setattr(ws_manager, "send_library_file_added", record)
    (root / "lamp.3mf").rename(root / "lantern.3mf")
    await _scan(db_session, folder)

    assert told == [{"id": file_id, "filename": "lantern.3mf"}]
