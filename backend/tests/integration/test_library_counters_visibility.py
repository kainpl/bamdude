"""Counters do not count files the caller cannot see (upstream #3201, G11 U4).

Folders are shared structure — every library reader sees their names. But the
numbers ON a folder were computed over everybody's files: a ``library:read_own``
caller read how many files others keep in a folder and when they last touched
them. The same leak sat on a file: ``duplicate_count`` in the list and the
duplicates panel of the detail counted, and named (file name, folder), other
users' copies of the same bytes.
"""

import secrets
from datetime import datetime

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret
OWN = ["library:read_own"]
SAME_BYTES = "a" * 64
OLD = datetime(2020, 1, 1)
THEIR_DAY = datetime(2031, 5, 5)
MY_DAY = datetime(2030, 3, 3)


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"lc_{username}", "permissions": permissions}
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


@pytest.fixture
async def shared(async_client, db_session):
    """One shared folder: Alice keeps one file in it, Bob two — one with Alice's bytes."""
    alice, alice_id = await _make_user(async_client, username="lc_alice", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="lc_bob", permissions=OWN)
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    folder = LibraryFolder(name="shared", created_at=OLD, updated_at=OLD, products=[product])
    other = LibraryFolder(name="bobs corner", created_at=OLD, updated_at=OLD)
    db_session.add_all([folder, other])
    await db_session.flush()

    def a_file(name, owner, when, *, digest=None, folder_id=None):
        return LibraryFile(
            filename=name,
            file_path=f"library/{secrets.token_hex(4)}_{name}",
            file_type="3mf",
            file_size=10,
            file_hash=digest,
            folder_id=folder_id or folder.id,
            created_by_id=owner,
            created_at=when,
            updated_at=when,
        )

    mine = a_file("mine.3mf", alice_id, MY_DAY, digest=SAME_BYTES)
    theirs = a_file("theirs.3mf", bob_id, THEIR_DAY)
    twin = a_file("bobs secret name.3mf", bob_id, THEIR_DAY, digest=SAME_BYTES, folder_id=other.id)
    db_session.add_all([mine, theirs, twin])
    await db_session.commit()
    return {"alice": alice, "folder": folder, "other": other, "product": product, "mine": mine, "twin": twin}


def _find(tree, folder_id):
    for node in tree:
        if node["id"] == folder_id:
            return node
        hit = _find(node["children"], folder_id)
        if hit:
            return hit
    return None


def _day(value: str | None) -> str | None:
    return value[:10] if value else None


# -- folders -----------------------------------------------------------------


async def test_the_folder_tree_counts_only_the_callers_files(async_client, shared):
    tree = (await async_client.get("/api/v1/library/folders", headers=shared["alice"])).json()

    node = _find(tree, shared["folder"].id)
    assert node["file_count"] == 1
    assert _day(node["latest_activity_at"]) == "2030-03-03"
    # A folder with none of her files is still there — folders are shared — and reads empty.
    corner = _find(tree, shared["other"].id)
    assert corner["name"] == "bobs corner" and corner["file_count"] == 0
    assert _day(corner["latest_activity_at"]) == "2020-01-01"


async def test_a_reader_of_everything_still_counts_everything(async_client, shared):
    tree = (await async_client.get("/api/v1/library/folders")).json()

    node = _find(tree, shared["folder"].id)
    assert node["file_count"] == 2
    assert _day(node["latest_activity_at"]) == "2031-05-05"


async def test_one_folder_counts_only_the_callers_files(async_client, shared):
    folder_id = shared["folder"].id

    own = (await async_client.get(f"/api/v1/library/folders/{folder_id}", headers=shared["alice"])).json()
    everything = (await async_client.get(f"/api/v1/library/folders/{folder_id}")).json()

    assert (own["file_count"], _day(own["latest_activity_at"])) == (1, "2030-03-03")
    assert (everything["file_count"], _day(everything["latest_activity_at"])) == (2, "2031-05-05")


async def test_a_products_folders_count_only_the_callers_files(async_client, shared):
    url = f"/api/v1/library/folders/by-product/{shared['product'].id}"

    own = (await async_client.get(url, headers=shared["alice"])).json()
    everything = (await async_client.get(url)).json()

    assert [(f["file_count"], _day(f["latest_activity_at"])) for f in own] == [(1, "2030-03-03")]
    assert [(f["file_count"], _day(f["latest_activity_at"])) for f in everything] == [(2, "2031-05-05")]


# -- duplicates --------------------------------------------------------------


async def test_the_file_list_does_not_count_another_users_copy(async_client, shared):
    own = (await async_client.get("/api/v1/library/files?include_root=false", headers=shared["alice"])).json()
    everything = (await async_client.get("/api/v1/library/files?include_root=false")).json()

    assert {f["filename"]: f["duplicate_count"] for f in own} == {"mine.3mf": 0}
    assert {f["filename"]: f["duplicate_count"] for f in everything}["mine.3mf"] == 1


async def test_the_duplicates_panel_does_not_name_another_users_copy(async_client, shared):
    url = f"/api/v1/library/files/{shared['mine'].id}"

    own = await async_client.get(url, headers=shared["alice"])
    everything = (await async_client.get(url)).json()

    assert own.status_code == 200, own.text
    assert own.json()["duplicate_count"] == 0 and not own.json()["duplicates"]
    assert "bobs secret name" not in own.text and "bobs corner" not in own.text
    assert everything["duplicate_count"] == 1
    assert everything["duplicates"][0]["filename"] == "bobs secret name.3mf"


async def test_the_callers_own_second_copy_is_still_a_duplicate(async_client, db_session, shared):
    alice_id = shared["mine"].created_by_id
    db_session.add(
        LibraryFile(
            filename="mine again.3mf",
            file_path="library/again.3mf",
            file_type="3mf",
            file_size=10,
            file_hash=SAME_BYTES,
            created_by_id=alice_id,
        )
    )
    await db_session.commit()

    detail = (await async_client.get(f"/api/v1/library/files/{shared['mine'].id}", headers=shared["alice"])).json()

    assert detail["duplicate_count"] == 1
    assert [d["filename"] for d in detail["duplicates"]] == ["mine again.3mf"]
