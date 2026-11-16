"""A MakerWorld import's metadata is read only by someone who can see the file.

``GET /makerworld/imports/{id}/meta`` asked for ``makerworld:view`` and nothing
else: a caller with ``library:read_own`` read the title, description, author and
source URL of another user's import by walking ids, and of an import in the
trash. Its neighbours (the two cover routes, both import lists) already resolve
the file through the library's own rule; this one now does too, so a hidden or
trashed file is the same 404 as a missing one.
"""

import secrets
from datetime import datetime

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret

OWN = ["library:read", "library:read_own", "makerworld:view"]
ALL = ["library:read", "library:read_all", "makerworld:view"]


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"mw_{username}", "permissions": permissions}
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


async def _import(db, *, owner: int | None, trashed: bool = False) -> LibraryFile:
    row = LibraryFile(
        filename="seed.3mf",
        file_path=f"library/{secrets.token_hex(4)}_seed.3mf",
        file_type="3mf",
        file_size=10,
        source_type="makerworld",
        source_url="https://makerworld.com/models/1400373",
        created_by_id=owner,
        deleted_at=datetime(2026, 1, 1) if trashed else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    db.add(LibraryFileMakerworldMeta(library_file_id=row.id, title="Secret prototype", author_name="bob"))
    await db.commit()
    return row


async def test_read_own_cannot_read_the_meta_of_another_users_import(async_client: AsyncClient, db_session):
    alice, _alice_id = await _make_user(async_client, username="mw_alice", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="mw_bob", permissions=OWN)
    theirs = await _import(db_session, owner=bob_id)

    response = await async_client.get(f"/api/v1/makerworld/imports/{theirs.id}/meta", headers=alice)

    assert response.status_code == 404, response.text
    assert "Secret prototype" not in response.text


async def test_read_own_reads_the_meta_of_its_own_import(async_client: AsyncClient, db_session):
    alice, alice_id = await _make_user(async_client, username="mw_alice2", permissions=OWN)
    mine = await _import(db_session, owner=alice_id)

    response = await async_client.get(f"/api/v1/makerworld/imports/{mine.id}/meta", headers=alice)

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Secret prototype"


async def test_read_all_reads_the_meta_of_anyones_import(async_client: AsyncClient, db_session):
    carol, _carol_id = await _make_user(async_client, username="mw_carol", permissions=ALL)
    _bob, bob_id = await _make_user(async_client, username="mw_bob3", permissions=OWN)
    theirs = await _import(db_session, owner=bob_id)

    response = await async_client.get(f"/api/v1/makerworld/imports/{theirs.id}/meta", headers=carol)

    assert response.status_code == 200, response.text


async def test_the_meta_of_a_trashed_import_is_hidden(async_client: AsyncClient, db_session):
    alice, alice_id = await _make_user(async_client, username="mw_alice4", permissions=ALL)
    trashed = await _import(db_session, owner=alice_id, trashed=True)

    response = await async_client.get(f"/api/v1/makerworld/imports/{trashed.id}/meta", headers=alice)

    assert response.status_code == 404, response.text


async def test_a_hidden_import_and_a_missing_one_answer_alike(async_client: AsyncClient, db_session):
    alice, _alice_id = await _make_user(async_client, username="mw_alice5", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="mw_bob5", permissions=OWN)
    theirs = await _import(db_session, owner=bob_id)

    hidden = await async_client.get(f"/api/v1/makerworld/imports/{theirs.id}/meta", headers=alice)
    missing = await async_client.get("/api/v1/makerworld/imports/987654/meta", headers=alice)

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
