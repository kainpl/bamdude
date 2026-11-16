"""A directory renamed or moved on a share keeps its folder row.

A folder's link to products is a standing rule (whatever lands in the folder
joins them), and it hangs on the folder ROW. The scan used to read a renamed
directory as "one folder vanished, another appeared": the files kept their rows
(the file rule), but the folder's own row -- and its product links -- went, and
the next file dropped into the directory inherited nothing.

The folder is recognised only on the evidence of the files that moved with it,
never by a name. A directory that came back empty, was split in two or received
two old directories' files is not guessed: it is handled as it always was.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_scan import LibraryScanJob
from backend.app.models.product import product_files, product_folders
from backend.app.services import library_file_photos as photos
from backend.app.services.library_scan import run_scan
from backend.app.services.product_sync import apply_folder_products
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_library_scan_follows_a_move import (
    _annotate,
    _annotations,
    _link,
    _plates_of,
    _product,
    _products_of,
    _row,
    _rows,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.usefixtures("library_worker")]


def _colour(index: int) -> dict:
    """Distinct bytes per file: the move rule identifies a file by its content."""
    return {1: [{"id": 1, "type": "PLA", "color": f"#{index:02X}0000", "used_g": str(index)}]}


@pytest.fixture
async def mount(db_session: AsyncSession, tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    folder = LibraryFolder(name="NAS", is_external=True, external_path=str(root), external_show_hidden=False)
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return root, folder


async def _scan(db: AsyncSession, folder: LibraryFolder, *, finished: bool = True) -> LibraryScanJob:
    job = LibraryScanJob(folder_id=folder.id, status="queued")
    db.add(job)
    await db.commit()
    await run_scan(job.id)
    await photos.drain()
    await db.refresh(job)
    if finished:
        assert job.status == "finished", job.error
    return job


async def _folders(db: AsyncSession) -> dict[str, LibraryFolder]:
    stmt = select(LibraryFolder).where(LibraryFolder.parent_id.is_not(None)).execution_options(populate_existing=True)
    return {row.name: row for row in (await db.execute(stmt)).scalars()}


async def _folder_products(db: AsyncSession, folder_id: int) -> set[int]:
    stmt = select(product_folders.c.product_id).where(product_folders.c.library_folder_id == folder_id)
    return set((await db.execute(stmt)).scalars())


async def _link_folder(db: AsyncSession, folder_id: int, product_id: int) -> None:
    await apply_folder_products(db, folder_id=folder_id, product_ids=[product_id])
    await db.commit()


async def test_a_renamed_directory_keeps_its_folder_row(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "wip" / "vase.3mf", _colour(2))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    file_ids = {r.filename: r.id for r in await _rows(db_session)}

    (root / "wip").rename(root / "finished")
    job = await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert {name: row.id for name, row in folders.items()} == {"finished": wip_id}
    kept = folders["finished"]
    assert kept.parent_id == folder.id and kept.is_external
    assert kept.external_path == str(root / "finished")
    rows = await _rows(db_session)
    assert {r.filename: r.id for r in rows} == file_ids
    assert {r.folder_id for r in rows} == {wip_id}
    assert (job.folders_added, job.folders_removed) == (0, 0)
    assert (job.files_added, job.files_removed) == (0, 0)


async def test_a_renamed_directory_keeps_its_product_link_and_a_later_file_inherits_it(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, wip_id, kit)
    lamp_id = (await _row(db_session, "lamp.3mf")).id
    assert await _products_of(db_session, lamp_id) == {kit}

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    assert await _folder_products(db_session, wip_id) == {kit}
    assert await _products_of(db_session, lamp_id) == {kit}

    write_routing_3mf(root / "finished" / "vase.3mf", _colour(2))
    await _scan(db_session, folder)

    vase = await _row(db_session, "vase.3mf")
    assert vase.folder_id == wip_id
    assert await _products_of(db_session, vase.id) == {kit}
    assert await _plates_of(db_session, vase.id) == {kit}


async def test_a_file_added_in_the_same_scan_as_the_rename_inherits_the_folders_products(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, wip_id, kit)

    (root / "wip").rename(root / "finished")
    write_routing_3mf(root / "finished" / "vase.3mf", _colour(2))
    await _scan(db_session, folder)

    vase = await _row(db_session, "vase.3mf")
    assert vase.folder_id == wip_id
    assert await _products_of(db_session, vase.id) == {kit}


async def test_subfolders_keep_their_rows_under_a_renamed_parent(db_session, mount):
    """A subfolder with files follows on their evidence; an empty one by its place under the proven parent."""
    root, folder = mount
    (root / "wip" / "parts").mkdir(parents=True)
    (root / "wip" / "spare").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "wip" / "parts" / "arm.3mf", _colour(2))
    await _scan(db_session, folder)
    before = {name: row.id for name, row in (await _folders(db_session)).items()}
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, before["spare"], kit)
    arm_id = (await _row(db_session, "arm.3mf")).id

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert {name: row.id for name, row in folders.items()} == {
        "finished": before["wip"],
        "parts": before["parts"],
        "spare": before["spare"],
    }
    assert folders["parts"].parent_id == before["wip"] and folders["spare"].parent_id == before["wip"]
    assert folders["parts"].external_path == str(root / "finished" / "parts")
    assert folders["spare"].external_path == str(root / "finished" / "spare")
    assert await _folder_products(db_session, before["spare"]) == {kit}
    arm = await _row(db_session, "arm.3mf")
    assert (arm.id, arm.folder_id) == (arm_id, before["parts"])


async def test_an_empty_directory_that_is_still_there_keeps_its_folder_row_when_a_file_goes(db_session, mount):
    """The folder pass removes rows of directories that are GONE — an empty one that exists keeps its row and link."""
    root, folder = mount
    (root / "spare").mkdir()
    write_routing_3mf(root / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "vase.3mf", _colour(2))
    await _scan(db_session, folder)
    spare_id = (await _folders(db_session))["spare"].id
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, spare_id, kit)

    (root / "vase.3mf").unlink()
    job = await _scan(db_session, folder)

    assert job.files_removed == 1 and job.folders_removed == 0
    assert (await _folders(db_session))["spare"].id == spare_id
    assert await _folder_products(db_session, spare_id) == {kit}


async def test_a_parent_with_no_files_of_its_own_follows_on_its_subfolders_files(db_session, mount):
    root, folder = mount
    (root / "wip" / "parts").mkdir(parents=True)
    write_routing_3mf(root / "wip" / "parts" / "arm.3mf", _colour(1))
    await _scan(db_session, folder)
    before = {name: row.id for name, row in (await _folders(db_session)).items()}

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    assert {name: row.id for name, row in (await _folders(db_session)).items()} == {
        "finished": before["wip"],
        "parts": before["parts"],
    }


async def test_a_directory_moved_into_another_keeps_its_row_under_the_new_parent(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    (root / "done").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "done" / "old.3mf", _colour(2))
    await _scan(db_session, folder)
    before = {name: row.id for name, row in (await _folders(db_session)).items()}
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, before["wip"], kit)
    lamp_id = (await _row(db_session, "lamp.3mf")).id

    (root / "wip").rename(root / "done" / "wip")
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert {name: row.id for name, row in folders.items()} == before
    assert folders["wip"].parent_id == before["done"]
    assert folders["wip"].external_path == str(root / "done" / "wip")
    assert await _folder_products(db_session, before["wip"]) == {kit}
    # The file moved WITH its folder: it stays in the same folder row, so nothing is joined or withdrawn.
    assert await _products_of(db_session, lamp_id) == {kit}
    assert (await _row(db_session, "lamp.3mf")).folder_id == before["wip"]


async def test_an_empty_renamed_directory_is_a_new_folder(db_session, mount):
    """Nothing in it says which directory it was."""
    root, folder = mount
    (root / "wip").mkdir()
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert folders["finished"].id != wip_id
    # As before this rule: with no file gone the folder pass does not run, and the old row waits for a scan that does.
    assert folders["wip"].id == wip_id and folders["wip"].external_path == str(root / "wip")


async def test_a_directory_whose_files_all_changed_is_a_new_folder(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id

    (root / "wip").rename(root / "finished")
    write_routing_3mf(root / "finished" / "lamp.3mf", _colour(9))
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert set(folders) == {"finished"} and folders["finished"].id != wip_id


async def test_a_split_directory_is_not_guessed(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "wip" / "vase.3mf", _colour(2))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, wip_id, kit)
    file_ids = {r.filename: r.id for r in await _rows(db_session)}

    (root / "left").mkdir()
    (root / "right").mkdir()
    (root / "wip" / "lamp.3mf").rename(root / "left" / "lamp.3mf")
    (root / "wip" / "vase.3mf").rename(root / "right" / "vase.3mf")
    (root / "wip").rmdir()
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert set(folders) == {"left", "right"}
    assert wip_id not in {row.id for row in folders.values()}
    assert await _folder_products(db_session, folders["left"].id) == set()
    assert await _folder_products(db_session, folders["right"].id) == set()
    assert {r.filename: r.id for r in await _rows(db_session)} == file_ids, "the files still follow by the file rule"


async def test_two_directories_merged_into_one_are_not_guessed(db_session, mount):
    root, folder = mount
    (root / "a").mkdir()
    (root / "b").mkdir()
    write_routing_3mf(root / "a" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "b" / "vase.3mf", _colour(2))
    await _scan(db_session, folder)
    before = {name: row.id for name, row in (await _folders(db_session)).items()}

    (root / "c").mkdir()
    (root / "a" / "lamp.3mf").rename(root / "c" / "lamp.3mf")
    (root / "b" / "vase.3mf").rename(root / "c" / "vase.3mf")
    (root / "a").rmdir()
    (root / "b").rmdir()
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert set(folders) == {"c"} and folders["c"].id not in before.values()


async def test_a_file_from_a_folder_that_stays_makes_the_new_directory_ambiguous(db_session, mount):
    """A file of ANOTHER folder landed in the new directory too: whose directory it is nobody can say."""
    root, folder = mount
    (root / "wip").mkdir()
    (root / "other").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "other" / "vase.3mf", _colour(2))
    write_routing_3mf(root / "other" / "stay.3mf", _colour(3))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id

    (root / "wip").rename(root / "finished")
    (root / "other" / "vase.3mf").rename(root / "finished" / "vase.3mf")
    await _scan(db_session, folder)

    folders = await _folders(db_session)
    assert set(folders) == {"finished", "other"} and folders["finished"].id != wip_id


async def test_files_keep_their_rows_notes_and_own_product_links_without_duplicates(db_session, mount):
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "wip" / "vase.3mf", _colour(2))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    kit, own = await _product(db_session, "Kit"), await _product(db_session, "Lamp")
    await _link_folder(db_session, wip_id, kit)
    lamp, vase = await _row(db_session, "lamp.3mf"), await _row(db_session, "vase.3mf")
    lamp_id, vase_id = lamp.id, vase.id
    await _link(db_session, lamp_id, own)
    await _link(db_session, lamp_id, kit)  # sync replaces: the file's set is {kit} ...
    from backend.app.services.product_sync import sync_product_for_file

    await sync_product_for_file(db_session, library_file_id=lamp_id, product_ids=[kit, own])
    await db_session.commit()
    await _annotate(db_session, lamp)

    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    assert {r.filename: (r.id, r.folder_id) for r in await _rows(db_session)} == {
        "lamp.3mf": (lamp_id, wip_id),
        "vase.3mf": (vase_id, wip_id),
    }
    assert await _products_of(db_session, lamp_id) == {kit, own}
    assert await _plates_of(db_session, lamp_id) == {kit, own}
    assert await _products_of(db_session, vase_id) == {kit}
    assert await _annotations(db_session, lamp_id) == (1, 1, 2)
    links = await db_session.scalar(select(func.count()).select_from(product_files))
    assert links == 3
    assert await db_session.scalar(select(func.count()).select_from(product_folders)) == 1


async def test_the_scan_after_a_directory_rename_changes_nothing(db_session, mount):
    root, folder = mount
    (root / "wip" / "parts").mkdir(parents=True)
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    write_routing_3mf(root / "wip" / "parts" / "arm.3mf", _colour(2))
    await _scan(db_session, folder)
    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)
    folders = {name: (row.id, row.parent_id, row.external_path) for name, row in (await _folders(db_session)).items()}
    files = [(r.id, r.file_path, r.folder_id) for r in await _rows(db_session)]

    job = await _scan(db_session, folder)

    assert {
        name: (row.id, row.parent_id, row.external_path) for name, row in (await _folders(db_session)).items()
    } == folders
    assert [(r.id, r.file_path, r.folder_id) for r in await _rows(db_session)] == files
    assert (job.files_added, job.files_removed, job.files_updated) == (0, 0, 0)
    assert (job.folders_added, job.folders_removed) == (0, 0)


async def test_an_empty_walk_still_removes_nothing(db_session, mount):
    """A share that blinked: no file, no folder row and no product link goes."""
    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    kit = await _product(db_session, "Kit")
    await _link_folder(db_session, wip_id, kit)

    (root / "wip" / "lamp.3mf").unlink()
    (root / "wip").rmdir()
    job = await _scan(db_session, folder)

    assert job.skipped_deletions is True
    assert (await _folders(db_session))["wip"].id == wip_id
    assert await _folder_products(db_session, wip_id) == {kit}
    assert len(await _rows(db_session)) == 1


async def test_open_tabs_are_told_the_scan_ended_so_the_folder_tree_is_read_again(db_session, mount, monkeypatch):
    """`library_scan_finished` is the event the frontend refreshes its folder tree on."""
    from backend.app.core.websocket import ws_manager

    root, folder = mount
    (root / "wip").mkdir()
    write_routing_3mf(root / "wip" / "lamp.3mf", _colour(1))
    await _scan(db_session, folder)
    wip_id = (await _folders(db_session))["wip"].id
    finished: list[dict] = []

    async def record(payload):
        finished.append(payload)

    monkeypatch.setattr(ws_manager, "send_library_scan_finished", record)
    (root / "wip").rename(root / "finished")
    await _scan(db_session, folder)

    assert [(p["folder_id"], p["status"], p["folders_added"], p["folders_removed"]) for p in finished] == [
        (folder.id, "finished", 0, 0)
    ]
    assert (await _folders(db_session))["finished"].id == wip_id
