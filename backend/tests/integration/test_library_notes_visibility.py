"""A file's notes are read and written only by someone who can see the file (G11 U2).

The four note routes checked that the row existed and nothing else: a caller with
``library:read_own`` could read the notes — and their authors' names — on another
user's file by id, write a note onto it, and do both on a file in the trash; the
404/200 difference was an oracle for which ids exist. They now resolve the file
through the library's own rule (``_ensure_library_file_visible``), so a hidden or
trashed file is the same 404 as a missing one.
"""

import secrets
from datetime import datetime

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret

OWN = ["library:read", "library:read_own", "library:notes_write"]


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"ln_{username}", "permissions": permissions}
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


async def _file(db, *, owner: int | None, trashed: bool = False, name: str = "part.3mf") -> LibraryFile:
    row = LibraryFile(
        filename=name,
        file_path=f"library/{secrets.token_hex(4)}_{name}",
        file_type="3mf",
        file_size=10,
        created_by_id=owner,
        deleted_at=datetime(2026, 1, 1) if trashed else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _note(db, file_id: int, user_id: int | None, body: str = "secret note") -> LibraryFileNote:
    note = LibraryFileNote(library_file_id=file_id, user_id=user_id, body=body)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def test_read_own_cannot_list_notes_of_another_users_file(async_client: AsyncClient, db_session):
    alice, alice_id = await _make_user(async_client, username="ln_alice", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="ln_bob", permissions=OWN)
    theirs = await _file(db_session, owner=bob_id)
    await _note(db_session, theirs.id, bob_id)

    response = await async_client.get(f"/api/v1/library/files/{theirs.id}/notes", headers=alice)

    assert response.status_code == 404, response.text


async def test_read_own_cannot_write_a_note_on_another_users_file(async_client: AsyncClient, db_session):
    alice, _alice_id = await _make_user(async_client, username="ln_alice2", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="ln_bob2", permissions=OWN)
    theirs = await _file(db_session, owner=bob_id)

    response = await async_client.post(
        f"/api/v1/library/files/{theirs.id}/notes", headers=alice, json={"body": "hello"}
    )

    assert response.status_code == 404, response.text
    assert (await async_client.get(f"/api/v1/library/files/{theirs.id}/notes")).json() == []


async def test_read_own_still_reads_and_writes_notes_on_its_own_file(async_client: AsyncClient, db_session):
    alice, alice_id = await _make_user(async_client, username="ln_alice3", permissions=OWN)
    mine = await _file(db_session, owner=alice_id)

    created = await async_client.post(f"/api/v1/library/files/{mine.id}/notes", headers=alice, json={"body": "mine"})
    assert created.status_code == 200, created.text
    listed = await async_client.get(f"/api/v1/library/files/{mine.id}/notes", headers=alice)
    assert listed.status_code == 200 and [n["body"] for n in listed.json()] == ["mine"]
    note_id = created.json()["id"]
    edited = await async_client.patch(f"/api/v1/library/notes/{note_id}", headers=alice, json={"body": "edited"})
    assert edited.status_code == 200 and edited.json()["body"] == "edited"
    assert (await async_client.delete(f"/api/v1/library/notes/{note_id}", headers=alice)).status_code == 200


async def test_notes_of_a_trashed_file_are_refused_like_the_file(async_client: AsyncClient, db_session):
    """The admin sees every file — but not one in the trash, on any library read."""
    trashed = await _file(db_session, owner=None, trashed=True)
    note = await _note(db_session, trashed.id, None)

    assert (await async_client.get(f"/api/v1/library/files/{trashed.id}/notes")).status_code == 404
    created = await async_client.post(f"/api/v1/library/files/{trashed.id}/notes", json={"body": "x"})
    assert created.status_code == 404
    assert (await async_client.patch(f"/api/v1/library/notes/{note.id}", json={"body": "y"})).status_code == 404
    assert (await async_client.delete(f"/api/v1/library/notes/{note.id}")).status_code == 404
    await db_session.refresh(note)
    assert note.body == "secret note"


async def test_an_author_who_no_longer_sees_the_file_cannot_edit_or_delete_the_note(
    async_client: AsyncClient, db_session
):
    """The note is Alice's, the file became Bob's: the file's rule comes first."""
    alice, alice_id = await _make_user(async_client, username="ln_alice4", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="ln_bob4", permissions=OWN)
    theirs = await _file(db_session, owner=bob_id)
    note = await _note(db_session, theirs.id, alice_id)

    edited = await async_client.patch(f"/api/v1/library/notes/{note.id}", headers=alice, json={"body": "y"})
    deleted = await async_client.delete(f"/api/v1/library/notes/{note.id}", headers=alice)

    assert edited.status_code == 404 and deleted.status_code == 404
    await db_session.refresh(note)
    assert note.body == "secret note"


async def test_a_hidden_file_and_a_missing_one_answer_alike(async_client: AsyncClient, db_session):
    alice, _alice_id = await _make_user(async_client, username="ln_alice5", permissions=OWN)
    _bob, bob_id = await _make_user(async_client, username="ln_bob5", permissions=OWN)
    theirs = await _file(db_session, owner=bob_id)

    hidden = await async_client.get(f"/api/v1/library/files/{theirs.id}/notes", headers=alice)
    missing = await async_client.get("/api/v1/library/files/987654/notes", headers=alice)

    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())


# --------------------------------------------------------------------------- #
# A note nobody wrote (m231 moved it, or its author was deleted) — review B-M5
# --------------------------------------------------------------------------- #

EDIT_OWN = [*OWN, "library:update_own"]
EDIT_ALL = [*OWN, "library:read_all", "library:update_all"]


async def test_whoever_may_edit_every_file_can_edit_and_delete_an_authorless_note(
    async_client: AsyncClient, db_session
):
    editor, _id = await _make_user(async_client, username="ln_editor", permissions=EDIT_ALL)
    file = await _file(db_session, owner=None)
    note = await _note(db_session, file.id, None, "moved by the upgrade")

    listed = await async_client.get(f"/api/v1/library/files/{file.id}/notes", headers=editor)
    assert listed.status_code == 200 and listed.json()[0]["can_edit"] is True
    edited = await async_client.patch(f"/api/v1/library/notes/{note.id}", headers=editor, json={"body": "tidied"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"] == "tidied" and edited.json()["can_edit"] is True
    assert (await async_client.delete(f"/api/v1/library/notes/{note.id}", headers=editor)).status_code == 200
    assert (await async_client.get(f"/api/v1/library/files/{file.id}/notes", headers=editor)).json() == []


async def test_a_file_s_owner_can_edit_and_delete_an_authorless_note_on_it(async_client: AsyncClient, db_session):
    alice, alice_id = await _make_user(async_client, username="ln_alice5", permissions=EDIT_OWN)
    mine = await _file(db_session, owner=alice_id)
    note = await _note(db_session, mine.id, None, "moved by the upgrade")

    listed = await async_client.get(f"/api/v1/library/files/{mine.id}/notes", headers=alice)
    assert listed.json()[0]["can_edit"] is True
    edited = await async_client.patch(f"/api/v1/library/notes/{note.id}", headers=alice, json={"body": "mine now"})
    assert edited.status_code == 200, edited.text
    assert (await async_client.delete(f"/api/v1/library/notes/{note.id}", headers=alice)).status_code == 200


async def test_an_authorless_note_on_a_file_the_caller_only_reads_stays_closed(async_client: AsyncClient, db_session):
    """A shared external file is read-only without ``update_all``; so is the note nobody wrote on it."""
    alice, _alice_id = await _make_user(async_client, username="ln_alice6", permissions=EDIT_OWN)
    shared = await _file(db_session, owner=None)
    shared.is_external = True
    await db_session.commit()
    note = await _note(db_session, shared.id, None)

    listed = await async_client.get(f"/api/v1/library/files/{shared.id}/notes", headers=alice)
    assert listed.status_code == 200 and listed.json()[0]["can_edit"] is False
    edited = await async_client.patch(f"/api/v1/library/notes/{note.id}", headers=alice, json={"body": "y"})
    deleted = await async_client.delete(f"/api/v1/library/notes/{note.id}", headers=alice)
    assert edited.status_code == 403 and deleted.status_code == 403


async def test_an_owner_who_may_not_edit_the_file_cannot_change_an_authorless_note(
    async_client: AsyncClient, db_session
):
    alice, alice_id = await _make_user(async_client, username="ln_alice7", permissions=OWN)
    mine = await _file(db_session, owner=alice_id)
    note = await _note(db_session, mine.id, None)

    edited = await async_client.patch(f"/api/v1/library/notes/{note.id}", headers=alice, json={"body": "y"})
    assert edited.status_code == 403


async def test_another_user_s_note_is_still_only_its_author_s(async_client: AsyncClient, db_session):
    editor, _id = await _make_user(async_client, username="ln_editor2", permissions=EDIT_ALL)
    _bob, bob_id = await _make_user(async_client, username="ln_bob8", permissions=OWN)
    file = await _file(db_session, owner=bob_id)
    note = await _note(db_session, file.id, bob_id)

    edited = await async_client.patch(f"/api/v1/library/notes/{note.id}", headers=editor, json={"body": "y"})
    deleted = await async_client.delete(f"/api/v1/library/notes/{note.id}", headers=editor)
    assert edited.status_code == 403 and deleted.status_code == 403
