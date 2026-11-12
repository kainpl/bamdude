"""What the product page asks of a linked file and folder (WS-13 E9 A03).

The server keeps no history of HOW a file joined a product: ``product_files`` holds
the pair, a folder link writes the same row onto each direct child
(``apply_folder_products``). So the page decides unlinking by the folder link that
holds NOW: ``in_linked_folder`` says whether the file sits in a folder linked to the
product — also for a file whose name the caller may not see, without its folder.
``is_3mf`` names the containers a card can be re-read from (``.3mf``, case aside —
the library's own rule), and ``folders`` lists the product's linked folders with
names under the library's folder rule: any library reader sees them, nobody else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from backend.app.core.auth import create_access_token
from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product
from backend.app.models.user import User
from backend.app.services.product_sync import apply_folder_products, sync_product_for_file
from backend.tests.auth_helpers import mint_test_access

pytestmark = pytest.mark.integration

_READ = Permission.PRODUCTS_READ.value


async def _user(db, username: str, permissions: list[str]) -> User:
    group = Group(name=f"grp-{username}", description="test", permissions=permissions)
    db.add(group)
    await db.flush()
    user = User(username=username, email=f"{username}@example.com", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _as(name: str) -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username=name)}"}


@pytest.fixture
async def shelf(db_session):
    """«Shelf»: folder «Linked» (linked to the product) and folder «Loose» (not).
    - direct — no folder, linked on its own, the reader-own user's;
    - loose — in «Loose», linked on its own;
    - inherited — in «Linked», joined through the folder;
    - double — in «Linked», linked on its own before the folder was;
    - stranger — in «Linked», somebody else's (hidden from the reader-own user);
    file names cover every extension the 3MF question meets."""
    reader_all = await _user(db_session, "fl_all", [_READ, Permission.LIBRARY_READ_ALL.value])
    reader_own = await _user(db_session, "fl_own", [_READ, Permission.LIBRARY_READ_OWN.value])
    await _user(db_session, "fl_none", [_READ])
    linked = LibraryFolder(name="Linked")
    loose = LibraryFolder(name="Loose")
    product = Product(name="Shelf")
    db_session.add_all([linked, loose, product])
    await db_session.flush()
    specs = {
        "direct": ("shelf.gcode.3mf", "gcode", None, reader_own.id),
        "loose": ("shelf-project.3mf", "3mf", loose.id, reader_own.id),
        "inherited": ("SHELF.GCODE.3MF", "gcode", linked.id, reader_own.id),
        "double": ("shelf.stl", "stl", linked.id, reader_own.id),
        "stranger": ("shelf-raw.gcode", "gcode", linked.id, reader_all.id),
        "step": ("shelf.step", "step", None, reader_own.id),
    }
    files: dict[str, int] = {}
    for label, (filename, file_type, folder_id, owner) in specs.items():
        f = LibraryFile(
            filename=filename,
            file_path=filename,
            file_size=1,
            file_type=file_type,
            file_metadata={},
            created_by_id=owner,
            folder_id=folder_id,
        )
        db_session.add(f)
        await db_session.flush()
        files[label] = f.id
    for label in ("direct", "loose", "double", "step"):
        await sync_product_for_file(db_session, library_file_id=files[label], product_ids=[product.id])
    await apply_folder_products(db_session, folder_id=linked.id, product_ids=[product.id])
    await db_session.commit()
    return {"product": product.id, "files": files, "linked": linked.id, "loose": loose.id}


async def _files(client, shelf, who: str = "test_admin") -> dict:
    r = await client.get(f"/api/v1/products/{shelf['product']}/files", headers=await _as(who))
    assert r.status_code == 200, r.text
    return r.json()


def _by_label(body: dict, shelf: dict) -> dict[str, dict]:
    label = {fid: name for name, fid in shelf["files"].items()}
    return {label[f["library_file_id"]]: f for f in body["files"]}


@pytest.mark.asyncio
async def test_is_3mf_follows_the_name_case_aside(async_client, shelf):
    files = _by_label(await _files(async_client, shelf), shelf)
    assert {label: f["is_3mf"] for label, f in files.items()} == {
        "direct": True,
        "loose": True,
        "inherited": True,
        "double": False,
        "stranger": False,
        "step": False,
    }


@pytest.mark.asyncio
async def test_is_3mf_is_known_for_a_file_without_access(async_client, shelf):
    files = _by_label(await _files(async_client, shelf, "fl_none"), shelf)
    assert all(f["hidden"] for f in files.values())
    assert files["direct"]["is_3mf"] is True and files["double"]["is_3mf"] is False


@pytest.mark.asyncio
async def test_in_linked_folder_is_the_folder_link_that_holds_now(async_client, shelf):
    files = _by_label(await _files(async_client, shelf), shelf)
    assert {label: f["in_linked_folder"] for label, f in files.items()} == {
        "direct": False,
        # A folder that is not linked to the product does not hold the file.
        "loose": False,
        "inherited": True,
        # Linked on its own AND through the folder — the server cannot tell them apart.
        "double": True,
        "stranger": True,
        "step": False,
    }


@pytest.mark.asyncio
async def test_in_linked_folder_holds_for_a_hidden_file_without_its_folder(async_client, shelf):
    files = _by_label(await _files(async_client, shelf, "fl_own"), shelf)
    stranger = files["stranger"]
    assert (stranger["hidden"], stranger["folder_id"], stranger["folder_name"]) == (True, None, None)
    assert stranger["in_linked_folder"] is True


@pytest.mark.asyncio
async def test_unlinking_the_folder_clears_its_files(committing_client, shelf):
    r = await committing_client.delete(f"/api/v1/products/{shelf['product']}/folders/{shelf['linked']}")
    assert r.status_code == 200, r.text
    body = await _files(committing_client, shelf)
    files = _by_label(body, shelf)
    # The folder link took its children with it — the doubly linked file too.
    assert set(files) == {"direct", "loose", "step"}
    assert all(not f["in_linked_folder"] for f in files.values())
    assert body["folders"] == []


@pytest.mark.parametrize(
    ("who", "names"),
    [("fl_all", ["Linked"]), ("fl_own", ["Linked"]), ("fl_none", [None])],
)
@pytest.mark.asyncio
async def test_folders_are_named_for_any_library_reader(async_client, shelf, who, names):
    folders = (await _files(async_client, shelf, who))["folders"]
    assert [f["name"] for f in folders] == names
    assert [f["folder_id"] for f in folders] == [shelf["linked"]]
    assert [f["hidden"] for f in folders] == [name is None for name in names]


@pytest.mark.asyncio
async def test_folders_named_first_by_name_hidden_after_by_id(async_client, db_session, shelf):
    extra = [LibraryFolder(name="B folder"), LibraryFolder(name="a folder")]
    db_session.add_all(extra)
    await db_session.flush()
    for folder in extra:
        await apply_folder_products(db_session, folder_id=folder.id, product_ids=[shelf["product"]])
    await db_session.commit()
    names = [f["name"] for f in (await _files(async_client, shelf))["folders"]]
    assert names == ["a folder", "B folder", "Linked"]
    hidden = (await _files(async_client, shelf, "fl_none"))["folders"]
    assert [f["folder_id"] for f in hidden] == sorted(f["folder_id"] for f in hidden)


@pytest.mark.asyncio
async def test_the_statements_do_not_grow_with_the_folders(async_client, db_session, shelf):
    selects: list[str] = []

    def count(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    async def measure() -> int:
        selects.clear()
        event.listen(db_session.bind.sync_engine, "before_cursor_execute", count)
        try:
            await _files(async_client, shelf)
        finally:
            event.remove(db_session.bind.sync_engine, "before_cursor_execute", count)
        return len(selects)

    await measure()  # warm-up: the first request of a session reads what later ones have cached
    one = await measure()
    extra = [LibraryFolder(name=f"F{n}") for n in range(3)]
    db_session.add_all(extra)
    await db_session.flush()
    for folder in extra:
        await apply_folder_products(db_session, folder_id=folder.id, product_ids=[shelf["product"]])
    await db_session.commit()
    assert await measure() == one
