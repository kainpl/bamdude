"""Integration tests for the user-authored library tags API (#1268 / G7-G1).

Covers the SYSTEM C tag catalog + assignment endpoints:
  * catalog CRUD (create / list / rename / delete)
  * 409 on case-insensitive duplicate create + rename collision
  * bulk-assign add / remove / replace
  * the ``tag_ids`` AND-filter on GET /library/files
  * ownership narrowing (``*_OWN`` file_count projection + bulk-assign scope)

These are DISTINCT from ``library_files.file_tags`` (computed system badges,
m036) and from ``print_archives.tags`` (archive CSV tags). This suite only
exercises the new ``library_tags`` / ``library_file_tags`` tables.

Auth is always-on in BamDude, so the default ``async_client`` carries an
admin JWT (full perms). Ownership tests build purpose-scoped ``*_own`` users
the same way as ``test_ownership_read_scoping.py``.
"""

import secrets

import pytest
from httpx import AsyncClient

from backend.app.core.auth import create_access_token

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_access_token(data={'sub': 'test_admin'})}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[str, int]:
    """Create a scoped user + group, return (token, user_id)."""
    headers = _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"lt_{username}", "permissions": permissions}
    )
    assert grp.status_code == 201, grp.text
    gid = grp.json()["id"]
    user = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [gid]},
    )
    assert user.status_code == 201, user.text
    uid = user.json()["id"]
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return login.json()["access_token"], uid


@pytest.fixture
async def file_factory(db_session):
    """Create library files directly (created_by_id supported for ownership)."""
    _counter = [0]

    async def _create_file(**kwargs):
        from backend.app.models.library import LibraryFile

        _counter[0] += 1
        counter = _counter[0]
        defaults = {
            "filename": f"tagfile_{counter}.3mf",
            "file_path": f"/test/path/tagfile_{counter}.3mf",
            "file_size": 1024,
            "file_type": "3mf",
        }
        defaults.update(kwargs)
        lib_file = LibraryFile(**defaults)
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)
        return lib_file

    return _create_file


# ============================ Catalog CRUD ============================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_list_rename_delete(async_client: AsyncClient):
    """Full catalog lifecycle: create → list → rename → delete."""
    # Create
    resp = await async_client.post("/api/v1/library/tags", json={"name": "  Toys  "})
    assert resp.status_code == 201, resp.text
    tag = resp.json()
    assert tag["name"] == "Toys"  # trimmed
    assert tag["file_count"] == 0
    tag_id = tag["id"]

    # List
    resp = await async_client.get("/api/v1/library/tags")
    assert resp.status_code == 200, resp.text
    names = {t["name"] for t in resp.json()}
    assert "Toys" in names

    # Rename
    resp = await async_client.patch(f"/api/v1/library/tags/{tag_id}", json={"name": "Kid-safe"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Kid-safe"

    # Delete
    resp = await async_client.delete(f"/api/v1/library/tags/{tag_id}")
    assert resp.status_code == 204, resp.text
    resp = await async_client.get("/api/v1/library/tags")
    assert all(t["id"] != tag_id for t in resp.json())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_duplicate_409(async_client: AsyncClient):
    """Case/space-insensitive duplicate create → 409."""
    resp = await async_client.post("/api/v1/library/tags", json={"name": "PETG"})
    assert resp.status_code == 201, resp.text
    # Different case + surrounding whitespace collapses onto the same name_key.
    resp = await async_client.post("/api/v1/library/tags", json={"name": " petg "})
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rename_collision_409_and_self_noop(async_client: AsyncClient):
    """Rename onto another tag's name → 409; renaming to own name is fine."""
    a = (await async_client.post("/api/v1/library/tags", json={"name": "Alpha"})).json()
    b = (await async_client.post("/api/v1/library/tags", json={"name": "Beta"})).json()

    # Collide Beta → alpha (case-insensitive)
    resp = await async_client.patch(f"/api/v1/library/tags/{b['id']}", json={"name": "alpha"})
    assert resp.status_code == 409, resp.text

    # Self-rename (same key, different case) allowed
    resp = await async_client.patch(f"/api/v1/library/tags/{a['id']}", json={"name": "ALPHA"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "ALPHA"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_delete_missing_404(async_client: AsyncClient):
    resp = await async_client.patch("/api/v1/library/tags/999999", json={"name": "Ghost"})
    assert resp.status_code == 404
    resp = await async_client.delete("/api/v1/library/tags/999999")
    assert resp.status_code == 404


# ============================ Bulk assign ============================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_add_remove_replace(async_client: AsyncClient, file_factory):
    """Bulk-assign add is idempotent; remove drops; replace clears+sets."""
    f1 = await file_factory()
    f2 = await file_factory()
    t1 = (await async_client.post("/api/v1/library/tags", json={"name": "red"})).json()
    t2 = (await async_client.post("/api/v1/library/tags", json={"name": "blue"})).json()

    # add t1 to both files
    resp = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f1.id, f2.id], "tag_ids": [t1["id"]], "action": "add"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files_updated"] == 2
    assert body["associations_added"] == 2

    # add again → idempotent (nothing new)
    resp = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f1.id, f2.id], "tag_ids": [t1["id"]], "action": "add"},
    )
    assert resp.json()["associations_added"] == 0

    # file_count reflects usage
    tags = {t["id"]: t for t in (await async_client.get("/api/v1/library/tags")).json()}
    assert tags[t1["id"]]["file_count"] == 2

    # remove t1 from f1
    resp = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f1.id], "tag_ids": [t1["id"]], "action": "remove"},
    )
    assert resp.json()["associations_removed"] == 1

    # replace f2's whole set with just t2
    resp = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f2.id], "tag_ids": [t2["id"]], "action": "replace"},
    )
    body = resp.json()
    assert body["associations_removed"] == 1  # t1 stripped
    assert body["associations_added"] == 1  # t2 set

    # replace with empty tag set clears everything on f2
    resp = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f2.id], "tag_ids": [], "action": "replace"},
    )
    assert resp.json()["associations_removed"] == 1
    tags = {t["id"]: t for t in (await async_client.get("/api/v1/library/tags")).json()}
    assert tags[t2["id"]]["file_count"] == 0


# ============================ tag_ids AND-filter ============================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_tag_ids_and_filter(async_client: AsyncClient, file_factory):
    """GET /library/files?tag_ids=… uses AND semantics across tags."""
    f1 = await file_factory(filename="only_a.3mf")
    f2 = await file_factory(filename="a_and_b.3mf")
    f3 = await file_factory(filename="only_b.3mf")
    ta = (await async_client.post("/api/v1/library/tags", json={"name": "A"})).json()
    tb = (await async_client.post("/api/v1/library/tags", json={"name": "B"})).json()

    await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f1.id, f2.id], "tag_ids": [ta["id"]], "action": "add"},
    )
    await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [f2.id, f3.id], "tag_ids": [tb["id"]], "action": "add"},
    )

    # single tag A → f1, f2
    resp = await async_client.get(f"/api/v1/library/files?tag_ids={ta['id']}")
    assert resp.status_code == 200, resp.text
    ids = {f["id"] for f in resp.json()}
    assert ids == {f1.id, f2.id}

    # A AND B → only f2 (cross-cutting; ignores folder/root scope)
    resp = await async_client.get(f"/api/v1/library/files?tag_ids={ta['id']}&tag_ids={tb['id']}")
    assert resp.status_code == 200, resp.text
    ids = {f["id"] for f in resp.json()}
    assert ids == {f2.id}

    # response carries the tags array (SYSTEM C), distinct from file_tags
    f2_row = next(f for f in resp.json() if f["id"] == f2.id)
    assert "tags" in f2_row
    assert {t["name"] for t in f2_row["tags"]} == {"A", "B"}
    assert "file_tags" in f2_row  # SYSTEM B still present + separate


# ============================ Ownership narrowing ============================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_tags_file_count_narrows_for_read_own(async_client: AsyncClient, file_factory):
    """A read_own caller's file_count only reflects their OWN files."""
    token_own, uid_own = await _make_user(async_client, username="lt_read_own", permissions=["library:read_own"])
    _, uid_other = await _make_user(async_client, username="lt_read_other", permissions=["library:read_own"])

    tag = (await async_client.post("/api/v1/library/tags", json={"name": "shared"})).json()
    my_file = await file_factory(created_by_id=uid_own)
    other_file = await file_factory(created_by_id=uid_other)

    # admin assigns the tag to BOTH files
    await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        json={"file_ids": [my_file.id, other_file.id], "tag_ids": [tag["id"]], "action": "add"},
    )

    # admin (read_all) sees file_count == 2
    admin_tags = {t["id"]: t for t in (await async_client.get("/api/v1/library/tags")).json()}
    assert admin_tags[tag["id"]]["file_count"] == 2

    # read_own caller sees only their own file in the count
    resp = await async_client.get("/api/v1/library/tags", headers={"Authorization": f"Bearer {token_own}"})
    assert resp.status_code == 200, resp.text
    own_tags = {t["id"]: t for t in resp.json()}
    assert own_tags[tag["id"]]["file_count"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_assign_ownership_scoping(async_client: AsyncClient, file_factory):
    """An update_own caller can only tag files they created; others skipped."""
    token_own, uid_own = await _make_user(
        async_client, username="lt_upd_own", permissions=["library:update_own", "library:read_own"]
    )
    _, uid_other = await _make_user(async_client, username="lt_upd_other", permissions=["library:read_own"])

    tag = (await async_client.post("/api/v1/library/tags", json={"name": "scoped"})).json()
    my_file = await file_factory(created_by_id=uid_own)
    other_file = await file_factory(created_by_id=uid_other)

    # update_own user tries to tag BOTH — only their own file is affected
    resp = await async_client.post(
        "/api/v1/library/tags/bulk-assign",
        headers={"Authorization": f"Bearer {token_own}"},
        json={"file_ids": [my_file.id, other_file.id], "tag_ids": [tag["id"]], "action": "add"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files_updated"] == 1  # other_file silently skipped
    assert body["associations_added"] == 1

    # admin confirms only my_file carries the tag
    resp = await async_client.get(f"/api/v1/library/files?tag_ids={tag['id']}")
    ids = {f["id"] for f in resp.json()}
    assert ids == {my_file.id}
