"""Moving a folder is checked on the server (upstream #3306, G11 U3).

``PUT /library/folders/{id}`` took any ``parent_id``: a parent that does not
exist (on SQLite the folder then vanished from the tree — nothing is its parent
and it is no root), a move INTO an external folder (a database-only row posing
as a directory of the share) and a move OF an external folder or a scanned
subfolder out from under its root (the scanner walks ``parent_id`` from the
root, so the next scan would file the same directory a second time). The cycle
check read the ancestors with no lock, so two opposite moves could both pass.

Renaming an external SUBFOLDER changed only the row: the scanner matches
subfolders by path, so the tree would show a name the share does not have.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryFolder

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def folder(db_session):
    async def _create(name: str, **kwargs) -> LibraryFolder:
        row = LibraryFolder(name=name, **kwargs)
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _create


@pytest.fixture
async def external(folder, tmp_path):
    """A mounted share with one scanned subfolder, as the scanner files them."""
    (tmp_path / "share" / "parts").mkdir(parents=True)
    root = await folder("share", is_external=True, external_path=str(tmp_path / "share"))
    sub = await folder("parts", parent_id=root.id, is_external=True, external_path=str(tmp_path / "share" / "parts"))
    return root, sub


async def _put(client: AsyncClient, folder_id: int, **body):
    return await client.put(f"/api/v1/library/folders/{folder_id}", json=body)


async def _parent(db, folder_id: int):
    return (
        await db.execute(
            select(LibraryFolder.parent_id)
            .where(LibraryFolder.id == folder_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _name(db, folder_id: int):
    return (await db.execute(select(LibraryFolder.name).where(LibraryFolder.id == folder_id))).scalar_one()


def _code(response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("error") if isinstance(detail, dict) else None


# -- what keeps working ------------------------------------------------------


async def test_a_folder_moves_into_another_and_back_to_the_root(async_client, db_session, folder):
    a = await folder("a")
    b = await folder("b")

    moved = await _put(async_client, a.id, parent_id=b.id)
    assert moved.status_code == 200, moved.text
    assert moved.json()["parent_id"] == b.id

    home = await _put(async_client, a.id, parent_id=0)
    assert home.status_code == 200, home.text
    assert home.json()["parent_id"] is None


async def test_a_plain_rename_still_works(async_client, db_session, folder):
    a = await folder("a")
    response = await _put(async_client, a.id, name="renamed")
    assert response.status_code == 200 and response.json()["name"] == "renamed"


# -- the parent --------------------------------------------------------------


async def test_a_missing_parent_is_404_and_nothing_changes(async_client, db_session, folder):
    a = await folder("a")

    response = await _put(async_client, a.id, parent_id=987654, name="other")

    assert response.status_code == 404, response.text
    assert _code(response) == "folder_parent_not_found"
    assert await _parent(db_session, a.id) is None
    assert await _name(db_session, a.id) == "a"


async def test_a_folder_does_not_move_into_an_external_folder(async_client, db_session, folder, external):
    root, sub = external
    a = await folder("a")

    into_root = await _put(async_client, a.id, parent_id=root.id)
    into_sub = await _put(async_client, a.id, parent_id=sub.id)

    assert into_root.status_code == 400 and _code(into_root) == "folder_parent_external"
    assert into_sub.status_code == 400 and _code(into_sub) == "folder_parent_external"
    assert await _parent(db_session, a.id) is None


# -- the folder itself -------------------------------------------------------


async def test_an_external_root_does_not_move(async_client, db_session, folder, external):
    root, _sub = external
    a = await folder("a")

    response = await _put(async_client, root.id, parent_id=a.id)

    assert response.status_code == 400, response.text
    assert _code(response) == "folder_external_fixed"
    assert await _parent(db_session, root.id) is None


async def test_a_scanned_subfolder_does_not_leave_its_root(async_client, db_session, folder, external):
    root, sub = external
    a = await folder("a")

    to_root = await _put(async_client, sub.id, parent_id=0)
    to_managed = await _put(async_client, sub.id, parent_id=a.id)

    assert to_root.status_code == 400 and _code(to_root) == "folder_external_fixed"
    assert to_managed.status_code == 400 and _code(to_managed) == "folder_external_fixed"
    assert await _parent(db_session, sub.id) == root.id


async def test_sending_an_external_folder_its_own_parent_is_no_move(async_client, db_session, external):
    """A client that sends the whole form back must not be refused."""
    root, sub = external

    assert (await _put(async_client, sub.id, parent_id=root.id)).status_code == 200
    assert (await _put(async_client, root.id, parent_id=0)).status_code == 200
    # …and the label of a mount point is ours to change: it names no directory.
    renamed = await _put(async_client, root.id, name="NAS")
    assert renamed.status_code == 200 and renamed.json()["name"] == "NAS"


async def test_an_external_subfolder_is_not_renamed_in_the_database_only(async_client, db_session, external):
    _root, sub = external

    response = await _put(async_client, sub.id, name="other")

    assert response.status_code == 400, response.text
    assert _code(response) == "folder_external_rename"
    assert await _name(db_session, sub.id) == "parts"
    # Its own name sent back is no rename.
    assert (await _put(async_client, sub.id, name="parts")).status_code == 200


# -- cycles ------------------------------------------------------------------


async def test_a_folder_is_not_its_own_parent(async_client, db_session, folder):
    a = await folder("a")
    response = await _put(async_client, a.id, parent_id=a.id)
    assert response.status_code == 400 and _code(response) == "folder_own_subtree"


async def test_a_folder_does_not_move_into_its_descendant(async_client, db_session, folder):
    a = await folder("a")
    b = await folder("b", parent_id=a.id)
    c = await folder("c", parent_id=b.id)

    response = await _put(async_client, a.id, parent_id=c.id)

    assert response.status_code == 400 and _code(response) == "folder_own_subtree"
    assert await _parent(db_session, a.id) is None


async def test_the_cycle_check_reads_after_the_lock(async_client, db_session, folder, monkeypatch):
    """Two opposite moves: A under B while B goes under A.

    The second move decides only after the first one committed — it takes the
    writer's lock BEFORE it reads the ancestors. The lock is where the other
    request finishes here: what it wrote must be what the check sees.
    """
    from backend.app.api.routes import library as routes

    a = await folder("a")
    b = await folder("b")
    real = routes.take_write_lock
    waited = []

    async def other_move_commits_while_we_wait(db, table, row_id):
        if not waited:
            waited.append(row_id)
            row = (await db.execute(select(LibraryFolder).where(LibraryFolder.id == b.id))).scalar_one()
            row.parent_id = a.id
            await db.flush()
        await real(db, table, row_id)

    monkeypatch.setattr(routes, "take_write_lock", other_move_commits_while_we_wait)

    response = await _put(async_client, a.id, parent_id=b.id)

    assert waited, "the move never took the writer's lock"
    assert response.status_code == 400, response.text
    assert _code(response) == "folder_own_subtree"


# -- the files' products do not move with a folder ---------------------------


async def test_moving_a_folder_asks_no_products_right_and_keeps_file_folders(async_client, db_session, folder):
    from backend.app.models.library import LibraryFile

    a = await folder("a")
    b = await folder("b")
    row = LibraryFile(filename="x.3mf", file_path="library/x.3mf", file_type="3mf", file_size=1, folder_id=a.id)
    db_session.add(row)
    await db_session.commit()

    assert (await _put(async_client, a.id, parent_id=b.id)).status_code == 200

    await db_session.refresh(row)
    assert row.folder_id == a.id
