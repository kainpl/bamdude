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
