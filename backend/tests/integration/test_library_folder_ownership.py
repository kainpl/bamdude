"""Library folders have an owner and can be shared (N56, upstream Bambuddy 6735e4556, #3201).

A ``library:read_own`` user sees their own folders, shared ones, those holding a file of theirs and
the folders above those (navigation only); any other folder is 404, like another user's file. They
add files and folders only to their own folders, shared ones and the root — every door that puts a
file into a chosen folder asks the same rule (404 unseen, 403 only seen). The owner renames, moves,
shares and deletes their folder (owner decision D3: sharing too); ``library:update_all`` any.
"""

from __future__ import annotations

import io
import secrets
import zipfile
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret
OWN = ["library:read_own", "library:upload", "library:update_own", "library:delete_own"]
STL = b"solid test\nendsolid test"


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"fo_{username}", "permissions": permissions}
    )
    assert grp.status_code == 201, grp.text
    user = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [grp.json()["id"]]},
    )
    assert user.status_code == 201, user.text
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user.json()["id"]


def _file(name: str, owner: int | None, folder_id: int | None) -> LibraryFile:
    return LibraryFile(
        filename=name,
        file_path=f"library/{secrets.token_hex(4)}_{name}",
        file_type="stl",
        file_size=10,
        folder_id=folder_id,
        created_by_id=owner,
    )


@pytest.fixture
async def world(async_client, db_session):
    """Bob's private tree with one of Alice's files deep inside, Bob's secret, a shared folder, Alice's own."""
    alice, alice_id = await _make_user(async_client, username="fo_alice", permissions=OWN)
    bob, bob_id = await _make_user(async_client, username="fo_bob", permissions=OWN)
    bob_root = LibraryFolder(name="bob root", created_by_id=bob_id)
    secret = LibraryFolder(name="bob secret", created_by_id=bob_id)
    everyone = LibraryFolder(name="everyone", created_by_id=bob_id, shared=True)
    alices = LibraryFolder(name="alice own", created_by_id=alice_id)
    db_session.add_all([bob_root, secret, everyone, alices])
    await db_session.flush()
    holder = LibraryFolder(name="holds alices file", parent_id=bob_root.id, created_by_id=bob_id)
    db_session.add(holder)
    await db_session.flush()
    alice_file = _file("alice.stl", alice_id, holder.id)
    bob_file = _file("bob.stl", bob_id, secret.id)
    alice_own_file = _file("alice-own.stl", alice_id, alices.id)
    db_session.add_all([alice_file, bob_file, alice_own_file])
    await db_session.commit()
    return SimpleNamespace(
        alice=alice,
        alice_id=alice_id,
        bob=bob,
        bob_id=bob_id,
        bob_root=bob_root,
        holder=holder,
        secret=secret,
        everyone=everyone,
        alices=alices,
        alice_file=alice_file,
        bob_file=bob_file,
    )


def _flat(tree) -> dict[int, dict]:
    out = {}
    stack = list(tree)
    while stack:
        node = stack.pop()
        out[node["id"]] = node
        stack.extend(node["children"])
    return out


# -- what a read_own user sees ------------------------------------------------


async def test_the_tree_holds_own_shared_and_the_way_to_my_file(async_client, world):
    tree = _flat((await async_client.get("/api/v1/library/folders", headers=world.alice)).json())

    assert set(tree) == {world.bob_root.id, world.holder.id, world.everyone.id, world.alices.id}
    assert tree[world.bob_root.id]["can_write"] is False, "a parent on the way is navigation only"
    shared = tree[world.everyone.id]
    assert (shared["shared"], shared["can_write"], shared["can_rename"], shared["can_share"]) == (
        True,
        True,
        False,
        False,
    )
    mine = tree[world.alices.id]
    assert (mine["created_by_id"], mine["can_rename"], mine["can_share"], mine["can_delete"]) == (
        world.alice_id,
        True,
        True,
        True,
    )


async def test_a_reader_of_everything_sees_every_folder(async_client, world):
    tree = _flat((await async_client.get("/api/v1/library/folders")).json())

    assert world.secret.id in tree and tree[world.secret.id]["can_write"] is True


async def test_another_users_private_folder_is_404_everywhere_it_is_named(async_client, world):
    sid = world.secret.id

    for url in (
        f"/api/v1/library/folders/{sid}",
        f"/api/v1/library/folders/{sid}/readme",
        f"/api/v1/library/files?folder_id={sid}",
    ):
        response = await async_client.get(url, headers=world.alice)
        assert response.status_code == 404, (url, response.text)
    assert (await async_client.get(f"/api/v1/library/folders/{sid}")).status_code == 200


async def test_stats_and_a_products_folders_count_only_visible_folders(async_client, db_session, world):
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    for folder in (world.secret, world.everyone):
        await db_session.refresh(folder, ["products"])
        folder.products.append(product)
    await db_session.commit()

    stats = (await async_client.get("/api/v1/library/stats", headers=world.alice)).json()
    linked = (await async_client.get(f"/api/v1/library/folders/by-product/{product.id}", headers=world.alice)).json()

    assert stats["total_folders"] == 4
    assert [f["id"] for f in linked] == [world.everyone.id]


async def test_the_product_page_hides_a_bound_folder_the_caller_does_not_see(async_client, db_session, world):
    carol, _carol_id = await _make_user(
        async_client, username="fo_carol", permissions=["library:read_own", "products:read"]
    )
    product = Product(name="Desk")
    db_session.add(product)
    await db_session.flush()
    for folder in (world.secret, world.everyone):
        await db_session.refresh(folder, ["products"])
        folder.products.append(product)
    await db_session.commit()

    folders = (await async_client.get(f"/api/v1/products/{product.id}/files", headers=carol)).json()["folders"]

    assert {(f["folder_id"], f["name"], f["hidden"]) for f in folders} == {
        (world.everyone.id, "everyone", False),
        (world.secret.id, None, True),
    }


# -- putting something into a folder ------------------------------------------


async def test_a_new_folder_goes_only_where_the_maker_may_write_and_is_theirs(async_client, world):
    async def make(parent_id):
        return await async_client.post(
            "/api/v1/library/folders", headers=world.alice, json={"name": "new", "parent_id": parent_id}
        )

    assert (await make(world.secret.id)).status_code == 404
    assert (await make(world.bob_root.id)).status_code == 403
    made = await make(world.everyone.id)
    assert made.status_code == 200, made.text
    assert (made.json()["created_by_id"], made.json()["shared"]) == (world.alice_id, False)


async def test_an_upload_and_a_move_ask_the_same_rule(async_client, world):
    async def upload(folder_id):
        return await async_client.post(
            f"/api/v1/library/files?folder_id={folder_id}",
            headers=world.alice,
            files={"file": ("part.stl", STL, "application/octet-stream")},
        )

    assert (await upload(world.secret.id)).status_code == 404
    assert (await upload(world.bob_root.id)).status_code == 403
    assert (await upload(world.everyone.id)).status_code == 200

    moved = await async_client.put(
        f"/api/v1/library/files/{world.alice_file.id}", headers=world.alice, json={"folder_id": world.secret.id}
    )
    assert moved.status_code == 404
    bulk = await async_client.post(
        "/api/v1/library/files/move",
        headers=world.alice,
        json={"file_ids": [world.alice_file.id], "folder_id": world.bob_root.id},
    )
    assert bulk.status_code == 403


async def test_a_zip_never_extracts_into_someone_elses_folder_of_the_same_name(async_client, db_session, world):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bob secret/part.stl", STL)
    payload = buf.getvalue()

    for _ in range(2):  # the second time two folders share the name: no 500
        response = await async_client.post(
            "/api/v1/library/files/extract-zip?preserve_structure=true",
            headers=world.alice,
            files={"file": ("pack.zip", payload, "application/zip")},
        )
        assert response.status_code == 200, response.text

    db_session.expire_all()
    named = (
        await db_session.scalars(
            select(LibraryFolder).where(LibraryFolder.name == "bob secret").order_by(LibraryFolder.id)
        )
    ).all()
    assert [(f.id == world.secret.id, f.created_by_id) for f in named] == [
        (True, world.bob_id),
        (False, world.alice_id),
    ]
    assert await db_session.scalar(select(func.count()).where(LibraryFile.folder_id == world.secret.id)) == 1


# -- changing a folder ----------------------------------------------------------


async def test_the_owner_renames_and_shares_their_folder_and_nobody_elses(async_client, world):
    put = lambda folder, headers, body: async_client.put(  # noqa: E731
        f"/api/v1/library/folders/{folder.id}", headers=headers, json=body
    )

    assert (await put(world.alices, world.alice, {"name": "renamed"})).status_code == 200
    assert (await put(world.everyone, world.alice, {"name": "mine now"})).status_code == 403
    assert (await put(world.secret, world.alice, {"name": "x"})).status_code == 404
    assert (await put(world.everyone, world.alice, {"shared": False})).status_code == 403

    shared = await put(world.alices, world.alice, {"shared": True})
    assert shared.status_code == 200 and shared.json()["shared"] is True
    bobs_tree = _flat((await async_client.get("/api/v1/library/folders", headers=world.bob)).json())
    assert world.alices.id in bobs_tree, "a shared folder is everyone's"

    assert (await put(world.secret, await _admin_headers(), {"shared": True})).status_code == 200


async def test_an_external_folder_stays_shared(async_client, db_session, world):
    mount = LibraryFolder(name="nas", is_external=True, external_path="/nowhere", shared=True)
    db_session.add(mount)
    await db_session.commit()

    response = await async_client.put(f"/api/v1/library/folders/{mount.id}", json={"shared": False})

    assert response.status_code == 400 and response.json()["detail"]["error"] == "folder_external_shared"


async def test_the_owner_deletes_a_folder_only_when_everything_inside_is_theirs(async_client, world):
    assert (
        await async_client.delete(f"/api/v1/library/folders/{world.alices.id}", headers=world.alice)
    ).status_code == 200
    # Bob owns the tree, but Alice's file is in it.
    refused = await async_client.delete(f"/api/v1/library/folders/{world.bob_root.id}", headers=world.bob)
    assert refused.status_code == 403 and "other users" in refused.json()["detail"]
    assert (
        await async_client.delete(f"/api/v1/library/folders/{world.bob_root.id}", headers=world.alice)
    ).status_code == 403
    assert (
        await async_client.delete(f"/api/v1/library/folders/{world.secret.id}", headers=world.alice)
    ).status_code == 404


# -- the other doors --------------------------------------------------------------


async def test_a_product_import_never_reuses_a_private_folder_named_like_the_product(db_session, world):
    from backend.app.models.user import User
    from backend.app.services.library_folder_access import FolderActor
    from backend.app.services.product_card import _import_destination

    private = LibraryFolder(name="Lamp", created_by_id=world.bob_id)
    db_session.add(private)
    await db_session.commit()
    alice = FolderActor(await db_session.get(User, world.alice_id), False)

    destination = await _import_destination(db_session, None, "Lamp", alice)
    again = await _import_destination(db_session, None, "Lamp", alice)

    assert destination.id != private.id
    assert (destination.parent_id, destination.created_by_id, destination.shared) == (None, None, True)
    assert again.id == destination.id, "the shared product folder is reused"


async def test_an_importers_root_folder_is_the_shared_one(db_session, world):
    from backend.app.models.user import User
    from backend.app.services.library_folder_access import FolderActor, writable_folder_named

    bobs = LibraryFolder(name="MakerWorld", created_by_id=world.bob_id)
    db_session.add(bobs)
    await db_session.commit()
    admin_like = FolderActor(await db_session.get(User, world.alice_id), True)

    folder, made = await writable_folder_named(db_session, "MakerWorld", None, admin_like, system=True)
    again, made_again = await writable_folder_named(db_session, "MakerWorld", None, admin_like, system=True)

    # Even an actor who may write into Bob's private one gets the shared import folder.
    assert made and folder.shared and folder.id != bobs.id
    assert (again.id, made_again) == (folder.id, False)


async def test_the_result_of_a_job_goes_beside_its_source_only_while_writable(db_session, world):
    from backend.app.models.user import User
    from backend.app.services.library_folder_access import FolderActor, result_folder_id

    alice = FolderActor(await db_session.get(User, world.alice_id), False)

    assert await result_folder_id(db_session, world.alices.id, alice) == world.alices.id
    assert await result_folder_id(db_session, world.everyone.id, alice) == world.everyone.id
    assert await result_folder_id(db_session, world.holder.id, alice) is None, "her file's folder, but bob's"
    assert await result_folder_id(db_session, 999_999, alice) is None


async def test_saving_an_archive_asks_the_librarys_rule_not_the_archives(
    async_client, world, archive_factory, printer_factory
):
    from backend.app.core.config import settings

    reader, _reader_id = await _make_user(
        async_client,
        username="fo_archivist",
        permissions=["archives:read_all", "library:read_own", "library:upload"],
    )
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="archive/x.3mf")
    target = settings.base_dir / "archive" / "x.3mf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PK\x03\x04")

    response = await async_client.post(
        f"/api/v1/archives/{archive.id}/save-to-library?folder_id={world.secret.id}", headers=reader
    )

    assert response.status_code == 404, response.text


async def test_a_printer_import_asks_the_same_rule(async_client, db_session, world, printer_factory):
    importer, importer_id = await _make_user(
        async_client, username="fo_importer", permissions=["library:read_own", "library:upload", "printers:files"]
    )
    db_session.add(_file("theirs-too.stl", importer_id, world.holder.id))  # bob's tree becomes visible to them
    await db_session.commit()
    printer = await printer_factory()

    async def into(folder_id):
        return await async_client.post(
            f"/api/v1/printers/{printer.id}/files/import-to-library",
            headers=importer,
            json={"paths": ["/cache/a.3mf"], "folder_id": folder_id},
        )

    assert (await into(world.secret.id)).status_code == 404
    assert (await into(world.bob_root.id)).status_code == 403


async def test_a_slice_lands_beside_its_source_only_while_the_slicer_may_write_there(
    db_session, world, monkeypatch, tmp_path
):
    from backend.app.api.routes import library as library_routes
    from backend.app.models.user import User
    from backend.app.services.library_folder_access import FolderActor
    from backend.app.services.slicer_api import SliceResult
    from backend.tests.integration.test_swap_library_ingest import _Request, a_3mf

    outputs = iter([a_3mf(tmp_path / "out" / "a.gcode.3mf", "n56-a"), a_3mf(tmp_path / "out" / "b.gcode.3mf", "n56-b")])

    async def slicer(db, **_kwargs):
        return SliceResult(
            content=next(outputs), print_time_seconds=60, filament_used_g=1.0, filament_used_mm=10.0
        ), False

    async def no_preview(db, content, **_kwargs):
        return content

    monkeypatch.setattr(library_routes, "_run_slicer_with_fallback", slicer)
    monkeypatch.setattr(library_routes, "sliced_preview", no_preview)
    alice = FolderActor(await db_session.get(User, world.alice_id), False)

    async def slice_from(folder_id):
        answer = await library_routes.slice_and_persist(
            db_session,
            model_bytes=b"model",
            model_filename="part.3mf",
            folder_id=folder_id,
            extra_metadata=None,
            request=_Request(),
            current_user_id=world.alice_id,
            folder_actor=alice,
        )
        return (await db_session.get(LibraryFile, answer.library_file_id, populate_existing=True)).folder_id

    assert await slice_from(world.alices.id) == world.alices.id
    assert await slice_from(world.holder.id) is None, "her file sat in bob's private folder: the root"
