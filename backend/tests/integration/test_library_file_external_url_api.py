"""A library file's own link (``library_files.external_url``, m232).

A file had only its provenance (``source_url``, written by the MakerWorld import
and the slicer); the operator could not attach a link of their own — a Printables
page, a supplier's datasheet. ``PUT /library/files/{id}/external-url`` writes
one: http/https only, capped, never fetched by the server. Reading follows the
file's visibility; writing needs what editing the file needs, on a file the
caller may see.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.app.models.library import LibraryFile
from backend.tests.integration.test_media_token import _make_user

pytestmark = pytest.mark.integration

LINK = "https://www.printables.com/model/12345-lamp?utm=x#files"


async def _row(db, *, name: str = "lamp.3mf", owner: int | None = None, **extra) -> LibraryFile:
    row = LibraryFile(
        filename=name,
        file_path=f"library/{name}",
        file_size=10,
        file_type=name.rsplit(".", 1)[-1].lower(),
        created_by_id=owner,
        **extra,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _as(jwt: str) -> dict:
    return {"Authorization": f"Bearer {jwt}"}


async def _stored(db, file_id: int) -> str | None:
    db.expire_all()
    return (await db.get(LibraryFile, file_id)).external_url


@pytest.mark.asyncio
async def test_the_link_is_stored_as_given_and_read_back_in_the_detail_and_the_list(async_client, db_session):
    file_id = (await _row(db_session)).id

    put = await async_client.put(f"/api/v1/library/files/{file_id}/external-url", json={"external_url": LINK})
    assert put.status_code == 200, put.text
    assert put.json()["external_url"] == LINK

    detail = await async_client.get(f"/api/v1/library/files/{file_id}")
    assert detail.json()["external_url"] == LINK
    listed = (await async_client.get("/api/v1/library/files")).json()
    items = listed["items"] if isinstance(listed, dict) else listed
    assert next(item for item in items if item["id"] == file_id)["external_url"] == LINK
    # The provenance beside it is another field and stays what it was.
    assert detail.json()["source_url"] is None


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_dropped_and_an_empty_value_clears_it(async_client, db_session):
    file_id = (await _row(db_session)).id
    url = f"/api/v1/library/files/{file_id}/external-url"

    assert (await async_client.put(url, json={"external_url": f"  {LINK}\n"})).json()["external_url"] == LINK
    assert (await async_client.put(url, json={"external_url": "   "})).json()["external_url"] is None
    assert await _stored(db_session, file_id) is None

    await async_client.put(url, json={"external_url": LINK})
    assert (await async_client.put(url, json={"external_url": None})).json()["external_url"] is None
    assert await _stored(db_session, file_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "www.printables.com/model/1",
        "//example.com/x",
        "http://",
        "https:///nohost",
        "http://exa mple.com/",
        "https://example.com/\nX-Injected: 1",
        "https://example.com/" + "a" * 2100,
    ],
)
async def test_anything_but_an_http_link_of_a_sane_length_is_refused(async_client, db_session, bad):
    file_id = (await _row(db_session, external_url=LINK)).id

    got = await async_client.put(f"/api/v1/library/files/{file_id}/external-url", json={"external_url": bad})

    assert got.status_code == 422, f"{bad!r} was accepted: {got.status_code} {got.text}"
    assert isinstance(got.json()["detail"], str), "a sentence the boundary can translate, not a pydantic list"
    assert await _stored(db_session, file_id) == LINK, "a refused link must leave the saved one alone"


@pytest.mark.asyncio
async def test_renaming_a_file_does_not_touch_its_link(async_client, db_session):
    file_id = (await _row(db_session, external_url=LINK)).id

    renamed = await async_client.put(f"/api/v1/library/files/{file_id}", json={"filename": "renamed.3mf"})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["external_url"] == LINK


@pytest.mark.asyncio
async def test_a_missing_or_trashed_file_takes_no_link(async_client, db_session):
    trashed = await _row(db_session, name="gone.3mf", deleted_at=datetime.now(timezone.utc))

    assert (
        await async_client.put(f"/api/v1/library/files/{trashed.id}/external-url", json={"external_url": LINK})
    ).status_code == 404
    assert await _stored(db_session, trashed.id) is None
    assert (
        await async_client.put("/api/v1/library/files/999999/external-url", json={"external_url": LINK})
    ).status_code == 404


@pytest.mark.asyncio
async def test_a_reader_of_own_files_neither_reads_nor_writes_another_users_link(async_client, db_session):
    """Somebody else's file and a missing one are the same 404 — also for the
    write, which must not confirm that the id exists."""
    jwt_a, uid_a = await _make_user(
        async_client, username="link_own_a", permissions=["library:read_own", "library:update_own"]
    )
    _, uid_b = await _make_user(async_client, username="link_own_b", permissions=["library:read_own"])
    mine = await _row(db_session, name="mine.3mf", owner=uid_a)
    theirs = await _row(db_session, name="theirs.3mf", owner=uid_b, external_url=LINK)

    own = await async_client.put(
        f"/api/v1/library/files/{mine.id}/external-url", headers=_as(jwt_a), json={"external_url": LINK}
    )
    assert own.status_code == 200, own.text

    read = await async_client.get(f"/api/v1/library/files/{theirs.id}", headers=_as(jwt_a))
    assert read.status_code == 404
    listed = (await async_client.get("/api/v1/library/files", headers=_as(jwt_a))).json()
    items = listed["items"] if isinstance(listed, dict) else listed
    assert [item["id"] for item in items] == [mine.id]

    write = await async_client.put(
        f"/api/v1/library/files/{theirs.id}/external-url",
        headers=_as(jwt_a),
        json={"external_url": "https://evil.example/"},
    )
    assert write.status_code == 404, f"another user's link was writable: {write.status_code}"
    assert await _stored(db_session, theirs.id) == LINK


@pytest.mark.asyncio
async def test_seeing_every_file_is_not_the_right_to_edit_another_users(async_client, db_session):
    jwt, _ = await _make_user(
        async_client, username="link_reader", permissions=["library:read_all", "library:update_own"]
    )
    _, uid_b = await _make_user(async_client, username="link_owner", permissions=["library:read_own"])
    theirs = await _row(db_session, name="theirs.3mf", owner=uid_b, external_url=LINK)
    ownerless = await _row(db_session, name="scanned.3mf", external_url=LINK)

    for file_id, name in ((theirs.id, "another user's"), (ownerless.id, "ownerless")):
        write = await async_client.put(
            f"/api/v1/library/files/{file_id}/external-url", headers=_as(jwt), json={"external_url": None}
        )
        assert write.status_code == 403, f"{name}: {write.status_code}"
        assert await _stored(db_session, file_id) == LINK


@pytest.mark.asyncio
async def test_a_reader_without_an_update_right_writes_nothing(async_client, db_session):
    jwt, uid = await _make_user(async_client, username="link_viewer", permissions=["library:read_all"])
    mine = await _row(db_session, name="mine.3mf", owner=uid)

    write = await async_client.put(
        f"/api/v1/library/files/{mine.id}/external-url", headers=_as(jwt), json={"external_url": LINK}
    )
    assert write.status_code == 403
    assert await _stored(db_session, mine.id) is None
