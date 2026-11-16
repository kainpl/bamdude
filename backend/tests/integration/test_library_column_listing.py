"""One level of the library, as a column of the File Manager asks for it (G11, Miller columns).

Each column of the columns view lists the DIRECT files of its own folder — the
root included — through the paged list endpoint, with the page's sort and
filters and a page number of its own. These pin the request a column sends:
it never reaches into a subfolder, a tag filter narrows the level instead of
replacing it (``folder_scope``), paging and the total are the server's, and a
``library:read_own`` caller's column neither shows nor counts another user's
files.
"""

import secrets
from datetime import datetime

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile, LibraryFileTag, LibraryFolder, LibraryTag
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret
WHEN = datetime(2030, 1, 1)


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"col_{username}", "permissions": permissions}
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
async def library(async_client, db_session):
    """root: a.stl (Alice), z.stl (Bob) · parts/: b.stl, c.stl (Alice), y.stl (Bob) · parts/small/: d.stl (Alice)."""
    alice, alice_id = await _make_user(async_client, username="col_alice", permissions=["library:read_own"])
    _bob, bob_id = await _make_user(async_client, username="col_bob", permissions=["library:read_own"])
    parts = LibraryFolder(name="parts", created_at=WHEN, updated_at=WHEN)
    db_session.add(parts)
    await db_session.flush()
    small = LibraryFolder(name="small", parent_id=parts.id, created_at=WHEN, updated_at=WHEN)
    tag = LibraryTag(name="keep", name_key="keep")
    db_session.add_all([small, tag])
    await db_session.flush()

    def a_file(name, owner, folder_id):
        return LibraryFile(
            filename=name,
            file_path=f"library/{secrets.token_hex(4)}_{name}",
            file_type="stl",
            file_size=10,
            folder_id=folder_id,
            created_by_id=owner,
            created_at=WHEN,
            updated_at=WHEN,
        )

    rows = {
        "a": a_file("a.stl", alice_id, None),
        "z": a_file("z.stl", bob_id, None),
        "b": a_file("b.stl", alice_id, parts.id),
        "c": a_file("c.stl", alice_id, parts.id),
        "y": a_file("y.stl", bob_id, parts.id),
        "d": a_file("d.stl", alice_id, small.id),
    }
    db_session.add_all(rows.values())
    await db_session.flush()
    # The tag sits on one file of each level, and on Bob's file in parts/.
    db_session.add_all([LibraryFileTag(file_id=rows[key].id, tag_id=tag.id) for key in ("a", "c", "y", "d")])
    await db_session.commit()
    return {"alice": alice, "parts": parts.id, "small": small.id, "tag": tag.id}


async def _column(async_client, headers, **params) -> tuple[list[str], dict]:
    query = {"page": 1, "per_page": 50, "sort_by": "name_asc", "include_root": "true", **params}
    response = await async_client.get("/api/v1/library/files", headers=headers, params=query)
    assert response.status_code == 200, response.text
    body = response.json()
    return [item["filename"] for item in body["items"]], body["meta"]


async def test_a_column_lists_the_direct_files_of_its_folder_only(async_client, library):
    admin = await _admin_headers()

    names, meta = await _column(async_client, admin, folder_id=library["parts"])
    assert names == ["b.stl", "c.stl", "y.stl"]  # not d.stl, one level down
    assert meta["total"] == 3

    names, meta = await _column(async_client, admin)  # the root column
    assert names == ["a.stl", "z.stl"]
    assert meta["total"] == 2


async def test_a_column_pages_on_the_server(async_client, library):
    admin = await _admin_headers()

    names, meta = await _column(async_client, admin, folder_id=library["parts"], per_page=2, page=2)

    assert names == ["y.stl"]
    assert (meta["total"], meta["current_page"], meta["last_page"]) == (3, 2, 2)


async def test_a_tag_filter_narrows_the_level_instead_of_replacing_it(async_client, library):
    admin = await _admin_headers()
    tagged = {"tag_ids": library["tag"], "folder_scope": "true"}

    names, meta = await _column(async_client, admin, folder_id=library["parts"], **tagged)
    assert names == ["c.stl", "y.stl"]
    assert meta["total"] == 2

    names, _meta = await _column(async_client, admin, **tagged)
    assert names == ["a.stl"]

    names, _meta = await _column(async_client, admin, folder_id=library["small"], **tagged)
    assert names == ["d.stl"]


async def test_a_search_narrows_the_level_too(async_client, library):
    admin = await _admin_headers()

    names, meta = await _column(async_client, admin, folder_id=library["parts"], q="c.st")

    assert names == ["c.stl"]
    assert meta["total"] == 1


async def test_a_read_own_column_neither_shows_nor_counts_another_users_files(async_client, library):
    alice = library["alice"]

    names, meta = await _column(async_client, alice, folder_id=library["parts"])
    assert names == ["b.stl", "c.stl"]
    assert meta["total"] == 2

    names, meta = await _column(async_client, alice)
    assert names == ["a.stl"]
    assert meta["total"] == 1

    names, meta = await _column(
        async_client, alice, folder_id=library["parts"], tag_ids=library["tag"], folder_scope="true"
    )
    assert names == ["c.stl"]
    assert meta["total"] == 1
