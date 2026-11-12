"""The library may not move a product's plates without the Workshop's right (WS-13 E13 B01, B02, B07).

A library user organises files; a product's plates are the Workshop's. Every library
write that changes which products a file or folder belongs to — an explicit
``product_ids``, an unlink, and a move whose destination brings another set of
products — asks ``products:update`` too, through the canonical gate called with the
request's own credentials (an API key passes its scope AND its owner's rights). A move
that keeps every file's products, and a new folder without products, stay library work.

The order routes that name a library file (an order from files, a plate line) answer
with the library's own gate: ``library_name_scope`` + ``file_name_visible`` — without
the right to read the library, not even the caller's own file is taken.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.core.auth import create_access_token, generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, product_files
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.services.product_sync import sync_product_for_file
from backend.tests.auth_helpers import bind_test_key, mint_test_access

pytestmark = pytest.mark.integration

_LIBRARY = [
    Permission.LIBRARY_UPLOAD.value,
    Permission.LIBRARY_UPDATE_ALL.value,
    Permission.LIBRARY_READ_ALL.value,
    Permission.PRODUCTS_READ.value,
]
_PRODUCTS_UPDATE = Permission.PRODUCTS_UPDATE.value
_NO_STOCK = {"from_finished": 0, "from_kits": 0}
_PLATES = {"plates": [{"index": 1, "printable_objects": {"1": "flask"}, "print_time_seconds": 60}]}


async def _user(db, username: str, permissions: list[str], *, is_active: bool = True) -> User:
    group = Group(name=f"grp-{username}", description="test", permissions=permissions)
    db.add(group)
    await db.flush()
    user = User(username=username, email=f"{username}@example.com", password_hash="x", role="user", is_active=is_active)
    user.groups.append(group)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _jwt(username: str) -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username=username)}"}


async def _key(db, owner: User | None, **flags) -> str:
    raw, key_hash, key_prefix = generate_api_key()
    key = APIKey(
        name=f"k-{key_prefix}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        user_id=owner.id if owner else None,
        **flags,
    )
    if owner is not None:
        await bind_test_key(db, key, owner)
    db.add(key)
    await db.commit()
    return raw


async def _file(db, name: str, *, folder: LibraryFolder | None = None, owner: User | None = None) -> LibraryFile:
    f = LibraryFile(
        filename=name,
        file_path=name,
        file_size=1,
        file_type="gcode",
        folder_id=folder.id if folder else None,
        created_by_id=owner.id if owner else None,
        file_metadata=_PLATES,
    )
    db.add(f)
    await db.flush()
    return f


async def _links(db, file_id: int) -> set[int]:
    rows = await db.execute(select(product_files.c.product_id).where(product_files.c.library_file_id == file_id))
    return set(rows.scalars().all())


@pytest.fixture
async def shelf(db_session):
    """Two products; folders F1 and F2 linked to P1, F3 to P2, F0 to none; file «a» in F1
    (P1), file «b» at the root linked to P1 directly, file «c» in F0 (no products)."""
    await _user(db_session, "lr_lib", _LIBRARY)
    await _user(db_session, "lr_both", [*_LIBRARY, _PRODUCTS_UPDATE])
    p1, p2 = Product(name="P1"), Product(name="P2")
    db_session.add_all([p1, p2])
    await db_session.flush()
    folders = {}
    for name, products in (("F0", []), ("F1", [p1]), ("F2", [p1]), ("F3", [p2])):
        folder = LibraryFolder(name=name)
        folder.products = products
        db_session.add(folder)
        folders[name] = folder
    await db_session.flush()
    a = await _file(db_session, "a.gcode.3mf", folder=folders["F1"])
    b = await _file(db_session, "b.gcode.3mf")
    c = await _file(db_session, "c.gcode.3mf", folder=folders["F0"])
    await sync_product_for_file(db_session, library_file_id=a.id, product_ids=[p1.id])
    await sync_product_for_file(db_session, library_file_id=b.id, product_ids=[p1.id])
    await db_session.commit()
    return {
        "p1": p1.id,
        "p2": p2.id,
        "folders": {k: v.id for k, v in folders.items()},
        "a": a.id,
        "b": b.id,
        "c": c.id,
    }


class TestExplicitLinks:
    @pytest.mark.asyncio
    async def test_a_new_folder_without_products_is_library_work(self, async_client: AsyncClient, shelf):
        lib = await _jwt("lr_lib")
        r = await async_client.post("/api/v1/library/folders/", json={"name": "Plain"}, headers=lib)
        assert r.status_code == 200, r.text
        r = await async_client.post("/api/v1/library/folders/", json={"name": "Empty", "product_ids": []}, headers=lib)
        assert r.status_code == 200, r.text
        r = await async_client.post(
            "/api/v1/library/folders/", json={"name": "Linked", "product_ids": [shelf["p1"]]}, headers=lib
        )
        assert r.status_code == 403, r.text
        r = await async_client.post(
            "/api/v1/library/folders/",
            json={"name": "Linked", "product_ids": [shelf["p1"]]},
            headers=await _jwt("lr_both"),
        )
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_editing_a_folder_asks_only_when_its_products_change(self, async_client: AsyncClient, shelf):
        """WS-13 E13 LIB-03: the set the folder ends with, compared with the one it has — an
        explicit list that changes nothing is library work."""
        lib = await _jwt("lr_lib")
        url = f"/api/v1/library/folders/{shelf['folders']['F0']}"
        assert (await async_client.put(url, json={"name": "Renamed"}, headers=lib)).status_code == 200
        assert (await async_client.put(url, json={"product_ids": None}, headers=lib)).status_code == 200
        assert (await async_client.put(url, json={"product_ids": []}, headers=lib)).status_code == 200
        linked = f"/api/v1/library/folders/{shelf['folders']['F1']}"
        assert (await async_client.put(linked, json={"product_ids": [shelf["p1"]]}, headers=lib)).status_code == 200
        assert (await async_client.put(linked, json={"product_ids": []}, headers=lib)).status_code == 403
        assert (await async_client.put(url, json={"product_ids": [shelf["p1"]]}, headers=lib)).status_code == 403
        r = await async_client.put(url, json={"product_ids": [shelf["p1"]]}, headers=await _jwt("lr_both"))
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_editing_a_file_asks_only_when_its_products_change(
        self, async_client: AsyncClient, db_session, shelf
    ):
        lib = await _jwt("lr_lib")
        url = f"/api/v1/library/files/{shelf['c']}"
        assert (await async_client.put(url, json={"notes": "x"}, headers=lib)).status_code == 200
        assert (await async_client.put(url, json={"product_ids": None}, headers=lib)).status_code == 200
        assert (await async_client.put(url, json={"product_ids": []}, headers=lib)).status_code == 200
        linked = f"/api/v1/library/files/{shelf['b']}"
        assert (await async_client.put(linked, json={"product_ids": [shelf["p1"]]}, headers=lib)).status_code == 200
        assert (await async_client.put(linked, json={"product_ids": []}, headers=lib)).status_code == 403
        assert (await async_client.put(url, json={"product_ids": [shelf["p1"]]}, headers=lib)).status_code == 403
        assert await _links(db_session, shelf["c"]) == set()
        r = await async_client.put(url, json={"product_ids": [shelf["p1"]]}, headers=await _jwt("lr_both"))
        assert r.status_code == 200, r.text
        assert await _links(db_session, shelf["c"]) == {shelf["p1"]}

    @pytest.mark.asyncio
    async def test_unlinking_asks_only_for_a_link_that_is_there(self, async_client: AsyncClient, db_session, shelf):
        lib = await _jwt("lr_lib")
        absent_file = f"/api/v1/library/files/{shelf['c']}/products/{shelf['p1']}"
        absent_folder = f"/api/v1/library/folders/{shelf['folders']['F0']}/products/{shelf['p1']}"
        assert (await async_client.delete(absent_file, headers=lib)).status_code == 204
        assert (await async_client.delete(absent_folder, headers=lib)).status_code == 204
        file_url = f"/api/v1/library/files/{shelf['a']}/products/{shelf['p1']}"
        folder_url = f"/api/v1/library/folders/{shelf['folders']['F1']}/products/{shelf['p1']}"
        assert (await async_client.delete(file_url, headers=lib)).status_code == 403
        assert (await async_client.delete(folder_url, headers=lib)).status_code == 403
        assert await _links(db_session, shelf["a"]) == {shelf["p1"]}
        both = await _jwt("lr_both")
        assert (await async_client.delete(file_url, headers=both)).status_code == 204
        assert (await async_client.delete(folder_url, headers=both)).status_code == 204


class TestAFolderOnTheShare:
    @pytest.mark.asyncio
    async def test_an_unknown_product_refuses_before_the_directory_is_made(
        self, async_client: AsyncClient, db_session, tmp_path
    ):
        """WS-13 E13 LIB-02: inside an external parent the folder is a real directory on the
        share — the products it is born linked to are checked before it is made, or a refusal
        leaves a directory behind that no row describes."""
        share = tmp_path / "share"
        share.mkdir()
        parent = LibraryFolder(name="Share", is_external=True, external_path=str(share), external_readonly=False)
        db_session.add(parent)
        await db_session.commit()
        r = await async_client.post(
            "/api/v1/library/folders/", json={"name": "New", "parent_id": parent.id, "product_ids": [999999]}
        )
        assert r.status_code == 404, r.text
        assert not (share / "New").exists()


class TestMoves:
    @pytest.mark.asyncio
    async def test_a_move_that_changes_a_files_products_asks(self, async_client: AsyncClient, db_session, shelf):
        lib = await _jwt("lr_lib")
        # A direct link to P1, moved to the root: the link would go.
        r = await async_client.put(f"/api/v1/library/files/{shelf['b']}", json={"folder_id": 0}, headers=lib)
        assert r.status_code == 403, r.text
        # P1 → a folder of P2.
        r = await async_client.put(
            f"/api/v1/library/files/{shelf['a']}", json={"folder_id": shelf["folders"]["F3"]}, headers=lib
        )
        assert r.status_code == 403, r.text
        file_a = await db_session.get(LibraryFile, shelf["a"])
        await db_session.refresh(file_a)
        assert file_a.folder_id == shelf["folders"]["F1"]
        assert await _links(db_session, shelf["a"]) == {shelf["p1"]}
        assert await _links(db_session, shelf["b"]) == {shelf["p1"]}

    @pytest.mark.asyncio
    async def test_a_move_that_keeps_a_files_products_is_library_work(
        self, async_client: AsyncClient, db_session, shelf
    ):
        r = await async_client.put(
            f"/api/v1/library/files/{shelf['a']}",
            json={"folder_id": shelf["folders"]["F2"]},
            headers=await _jwt("lr_lib"),
        )
        assert r.status_code == 200, r.text
        assert await _links(db_session, shelf["a"]) == {shelf["p1"]}

    @pytest.mark.asyncio
    async def test_the_project_right_moves_and_relinks(self, async_client: AsyncClient, db_session, shelf):
        r = await async_client.put(
            f"/api/v1/library/files/{shelf['b']}", json={"folder_id": 0}, headers=await _jwt("lr_both")
        )
        assert r.status_code == 200, r.text
        assert await _links(db_session, shelf["b"]) == set()

    @pytest.mark.asyncio
    async def test_a_batch_with_one_changing_file_moves_nothing(self, async_client: AsyncClient, db_session, shelf):
        body = {"file_ids": [shelf["a"], shelf["c"]], "folder_id": shelf["folders"]["F2"]}
        r = await async_client.post("/api/v1/library/files/move", json=body, headers=await _jwt("lr_lib"))
        assert r.status_code == 403, r.text
        for key, folder in (("a", "F1"), ("c", "F0")):
            row = await db_session.get(LibraryFile, shelf[key])
            await db_session.refresh(row)
            assert row.folder_id == shelf["folders"][folder]
        assert await _links(db_session, shelf["c"]) == set()
        r = await async_client.post("/api/v1/library/files/move", json=body, headers=await _jwt("lr_both"))
        assert r.status_code == 200, r.text
        assert r.json()["moved"] == 2
        assert await _links(db_session, shelf["c"]) == {shelf["p1"]}

    @pytest.mark.asyncio
    async def test_a_batch_that_keeps_every_files_products_is_library_work(
        self, async_client: AsyncClient, db_session, shelf
    ):
        body = {"file_ids": [shelf["a"]], "folder_id": shelf["folders"]["F2"]}
        r = await async_client.post("/api/v1/library/files/move", json=body, headers=await _jwt("lr_lib"))
        assert r.status_code == 200, r.text
        assert r.json()["moved"] == 1


class TestApiKeys:
    """B02: the second check is the canonical gate — a key passes its scope AND its owner's
    rights, by either header, and a refusal is a 403, never a 500."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["x-api-key", "bearer"])
    async def test_a_key_needs_its_scope_and_its_owners_right(
        self, async_client: AsyncClient, db_session, shelf, header
    ):
        # Exercise the key alone, not the client's independent admin session.
        # An invalid/deactivated key must not leave an unrelated valid Bearer
        # credential available to the existing multi-credential fallback.
        if header == "x-api-key":
            async_client.headers.pop("Authorization", None)
        owner_with = await _user(db_session, f"lr_kw_{header}", [*_LIBRARY, _PRODUCTS_UPDATE])
        owner_without = await _user(db_session, f"lr_ko_{header}", _LIBRARY)
        gone = await _user(db_session, f"lr_kg_{header}", [*_LIBRARY, _PRODUCTS_UPDATE])

        def auth(raw: str) -> dict:
            return {"X-API-Key": raw} if header == "x-api-key" else {"Authorization": f"Bearer {raw}"}

        url = f"/api/v1/library/files/{shelf['a']}/products/{shelf['p1']}"
        # Both scope flags default to True on a new key: switched off explicitly.
        no_scope = await _key(db_session, owner_with, can_manage_library=True, can_manage_projects=False)
        r = await async_client.delete(url, headers=auth(no_scope))
        assert r.status_code == 403, r.text
        owner_lacks = await _key(db_session, owner_without, can_manage_library=True, can_manage_projects=True)
        r = await async_client.delete(url, headers=auth(owner_lacks))
        assert r.status_code == 403, r.text
        dead_owner = await _key(db_session, gone, can_manage_library=True, can_manage_projects=True)
        gone.is_active = False
        await db_session.commit()
        r = await async_client.delete(url, headers=auth(dead_owner))
        assert r.status_code in (401, 403), r.text
        assert await _links(db_session, shelf["a"]) == {shelf["p1"]}
        allowed = await _key(db_session, owner_with, can_manage_library=True, can_manage_projects=True)
        r = await async_client.delete(url, headers=auth(allowed))
        assert r.status_code == 204, r.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", ["x-api-key", "bearer"])
    async def test_a_key_without_the_orders_scope_moves_nothing_that_relinks(
        self, async_client: AsyncClient, db_session, shelf, header
    ):
        """L.2 B01 / R03: a move that changes a file's products, sent with an API key — the
        second check asks the KEY's scope, and a refusal moves nothing."""
        owner = await _user(db_session, f"lr_km_{header}", [*_LIBRARY, _PRODUCTS_UPDATE])

        def auth(raw: str) -> dict:
            return {"X-API-Key": raw} if header == "x-api-key" else {"Authorization": f"Bearer {raw}"}

        body = {"file_ids": [shelf["c"]], "folder_id": shelf["folders"]["F2"]}
        no_scope = await _key(db_session, owner, can_manage_library=True, can_manage_projects=False)
        r = await async_client.post("/api/v1/library/files/move", json=body, headers=auth(no_scope))
        assert r.status_code == 403, r.text
        row = await db_session.get(LibraryFile, shelf["c"])
        await db_session.refresh(row)
        assert row.folder_id == shelf["folders"]["F0"]
        assert await _links(db_session, shelf["c"]) == set()
        allowed = await _key(db_session, owner, can_manage_library=True, can_manage_projects=True)
        r = await async_client.post("/api/v1/library/files/move", json=body, headers=auth(allowed))
        assert r.status_code == 200, r.text
        assert await _links(db_session, shelf["c"]) == {shelf["p1"]}


class TestOrderFileIntake:
    """B07: the order routes that name a library file answer with the library's gate."""

    @pytest.fixture
    async def files(self, db_session):
        me = await _user(
            db_session,
            "lr_own",
            [
                Permission.ORDERS_READ.value,
                Permission.ORDERS_CREATE.value,
                Permission.ORDERS_UPDATE.value,
                Permission.PRODUCTS_READ.value,
                Permission.LIBRARY_READ_OWN.value,
            ],
        )
        other = await _user(db_session, "lr_other", [Permission.ORDERS_READ.value])
        await _user(
            db_session,
            "lr_nolib",
            [
                Permission.ORDERS_READ.value,
                Permission.ORDERS_CREATE.value,
                Permission.ORDERS_UPDATE.value,
                Permission.PRODUCTS_READ.value,
            ],
        )
        nolib = (await db_session.execute(select(User).where(User.username == "lr_nolib"))).scalar_one()
        own = await _file(db_session, "own.gcode.3mf", owner=me)
        nolib_own = await _file(db_session, "nolib-own.gcode.3mf", owner=nolib)
        foreign = await _file(db_session, "foreign.gcode.3mf", owner=other)
        catalog = Product(name="Catalog")
        order = Project(name="O")
        db_session.add_all([catalog, order])
        await db_session.commit()
        return {
            "own": own.id,
            "nolib_own": nolib_own.id,
            "foreign": foreign.id,
            "catalog": catalog.id,
            "order": order.id,
            "me": me,
            "nolib": nolib,
        }

    @staticmethod
    def _plate(file_id: int) -> dict:
        # Taking nothing from the shelf, as a client without ``stock:move`` sends it (O06).
        return {
            "kind": "plate",
            "library_file_id": file_id,
            "plate_index": 1,
            "copies": 1,
            "stock": {"from_finished": 0, "from_kits": 0},
        }

    @pytest.mark.asyncio
    async def test_without_the_library_right_not_even_ones_own_file(self, async_client: AsyncClient, files):
        headers = await _jwt("lr_nolib")
        r = await async_client.post(
            f"/api/v1/projects/{files['order']}/lines/batch",
            json={"lines": [self._plate(files["nolib_own"])]},
            headers=headers,
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "Library file not found"
        body = {"kind": "plates", "library_file_id": files["nolib_own"], "plates": [{"plate_index": 1, "copies": 1}]}
        r = await async_client.post("/api/v1/projects/from-files", json=body, headers=headers)
        assert r.status_code == 404, r.text

    @pytest.mark.asyncio
    async def test_read_own_takes_its_own_file_only(self, async_client: AsyncClient, files):
        url = f"/api/v1/projects/{files['order']}/lines/batch"
        headers = await _jwt("lr_own")
        r = await async_client.post(url, json={"lines": [self._plate(files["foreign"])]}, headers=headers)
        assert r.status_code == 404, r.text
        r = await async_client.post(url, json={"lines": [self._plate(999999)]}, headers=headers)
        assert r.status_code == 404, r.text
        r = await async_client.post(url, json={"lines": [self._plate(files["own"])]}, headers=headers)
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_a_refused_file_line_leaves_the_whole_batch_unwritten(
        self, async_client: AsyncClient, db_session, files
    ):
        # No stock asked: the shelf is stock:move's (WS-13 E13), and this test is about the library.
        lines = [
            {"kind": "product", "product_id": files["catalog"], "quantity": 1, "stock": _NO_STOCK},
            self._plate(files["foreign"]),
        ]
        r = await async_client.post(
            f"/api/v1/projects/{files['order']}/lines/batch", json={"lines": lines}, headers=await _jwt("lr_own")
        )
        assert r.status_code == 404, r.text
        count = await db_session.scalar(
            select(func.count()).select_from(ProjectLine).where(ProjectLine.project_id == files["order"])
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_a_catalog_product_needs_no_library_right(self, async_client: AsyncClient, files):
        headers = await _jwt("lr_nolib")
        r = await async_client.post(
            f"/api/v1/projects/{files['order']}/lines/batch",
            json={"lines": [{"kind": "product", "product_id": files["catalog"], "quantity": 1, "stock": _NO_STOCK}]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        r = await async_client.post(
            f"/api/v1/projects/{files['order']}/lines",
            json={"product_id": files["catalog"], "quantity": 1},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_an_api_key_takes_a_file_only_with_its_library_scope_and_its_owners_right(
        self, async_client: AsyncClient, db_session, files
    ):
        reader = await _user(
            db_session, "lr_key_reader", [Permission.ORDERS_UPDATE.value, Permission.LIBRARY_READ_ALL.value]
        )
        blind = await _user(db_session, "lr_key_blind", [Permission.ORDERS_UPDATE.value])
        url = f"/api/v1/projects/{files['order']}/lines/batch"
        body = {"lines": [self._plate(files["foreign"])]}
        no_scope = await _key(db_session, reader, can_manage_projects=True, can_read_status=False)
        assert (await async_client.post(url, json=body, headers={"X-API-Key": no_scope})).status_code == 404
        owner_blind = await _key(db_session, blind, can_manage_projects=True, can_read_status=True)
        assert (await async_client.post(url, json=body, headers={"X-API-Key": owner_blind})).status_code == 404
        allowed = await _key(db_session, reader, can_manage_projects=True, can_read_status=True)
        r = await async_client.post(url, json=body, headers={"X-API-Key": allowed})
        assert r.status_code == 200, r.text
