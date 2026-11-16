"""``GET /library/files/{id}/image`` — the full picture behind the File Manager's image viewer.

An image in the library had a 256 px thumbnail and nothing that could open it.
The viewer is an ``<img>``, which cannot send a header, so the route takes the
MEDIA token (or the ordinary headers) and then applies the library file's own
visibility rule — the same one the thumbnail beside it uses. It serves the
types a browser draws in an ``<img>`` and nothing else: it is not a second
download route for models and G-code behind a URL token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.models.library import LibraryFile
from backend.tests.auth_helpers import mint_test_authority
from backend.tests.integration.test_media_token import _make_user, _mint, _raw_get

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"picture-bytes"


async def _image_row(db, tmp_path: Path, *, name: str = "photo.png", owner: int | None = None, **extra) -> LibraryFile:
    """A library row whose bytes are on disk under ``settings.base_dir`` (= ``tmp_path``)."""
    relative = f"library/{name}"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    row = LibraryFile(
        filename=name,
        file_path=relative,
        file_size=len(PNG),
        file_type=name.rsplit(".", 1)[-1].lower(),
        created_by_id=owner,
        **extra,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _admin_media_token() -> str:
    from backend.app.core.auth import create_media_token

    return await create_media_token(authority=await mint_test_authority(username="test_admin"))


@pytest.mark.asyncio
async def test_an_img_reaches_the_picture_with_only_a_media_token(committing_client, db_session, tmp_path):
    """From a client with NO Authorization header: the middleware must let the
    request reach the route's own gate, and the gate must take the token."""
    from backend.app.main import app

    file_id = (await _image_row(db_session, tmp_path)).id
    url = f"/api/v1/library/files/{file_id}/image"
    token = await _admin_media_token()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        assert (await anonymous.get(url)).status_code == 401, "no token is still no entry"
        served = await anonymous.get(url, params={"token": token})
        assert served.status_code == 200, served.text
        assert served.content == PNG
        assert served.headers["content-type"] == "image/png"
        # The neighbouring download stays a bearer read: a URL token buys no model file.
        assert (
            await anonymous.get(f"/api/v1/library/files/{file_id}/download", params={"token": token})
        ).status_code == 401

    # A fetch with ordinary headers works too.
    assert (await committing_client.get(url)).status_code == 200


@pytest.mark.asyncio
async def test_the_types_come_back_under_their_own_media_type(committing_client, db_session, tmp_path):
    for name, media_type in (
        ("a.jpg", "image/jpeg"),
        ("b.JPEG", "image/jpeg"),
        ("c.gif", "image/gif"),
        ("d.webp", "image/webp"),
        ("e.bmp", "image/bmp"),
    ):
        file_id = (await _image_row(db_session, tmp_path, name=name)).id
        got = await committing_client.get(f"/api/v1/library/files/{file_id}/image")
        assert got.status_code == 200, (name, got.text)
        assert got.headers["content-type"] == media_type, name


@pytest.mark.asyncio
async def test_it_serves_pictures_and_nothing_else(committing_client, db_session, tmp_path):
    """Not a download route with a URL token: a model, a G-code file, an SVG (a
    document a browser would run) and a type no browser draws are all refused."""
    token = await _admin_media_token()
    for name in ("model.3mf", "job.gcode", "part.stl", "drawing.svg", "scan.tiff", "noextension"):
        file_id = (await _image_row(db_session, tmp_path, name=name)).id
        got = await committing_client.get(f"/api/v1/library/files/{file_id}/image", params={"token": token})
        assert got.status_code == 404, f"{name} was served: {got.status_code}"

    assert (await committing_client.get("/api/v1/library/files/999999/image")).status_code == 404


@pytest.mark.asyncio
async def test_a_trashed_picture_or_one_without_bytes_is_not_served(committing_client, db_session, tmp_path):
    from datetime import datetime, timezone

    trashed = await _image_row(db_session, tmp_path, name="gone.png", deleted_at=datetime.now(timezone.utc))
    assert (await committing_client.get(f"/api/v1/library/files/{trashed.id}/image")).status_code == 404

    ghost = LibraryFile(filename="ghost.png", file_path="library/ghost.png", file_size=1, file_type="png")
    db_session.add(ghost)
    await db_session.commit()
    assert (await committing_client.get(f"/api/v1/library/files/{ghost.id}/image")).status_code == 404


@pytest.mark.asyncio
async def test_a_reader_of_own_files_never_sees_another_users_picture(async_client, db_session, tmp_path):
    """The reason the token names its user. Somebody else's file and a missing
    one are the same 404."""
    jwt_a, uid_a = await _make_user(async_client, username="img_own_a", permissions=["library:read_own"])
    _, uid_b = await _make_user(async_client, username="img_own_b", permissions=["library:read_own"])
    mine = await _image_row(db_session, tmp_path, name="mine.png", owner=uid_a)
    theirs = await _image_row(db_session, tmp_path, name="theirs.png", owner=uid_b)
    token = await _mint(async_client, jwt_a)

    own = await _raw_get(async_client, f"/api/v1/library/files/{mine.id}/image?token={token}")
    assert own.status_code == 200, own.text
    foreign = await _raw_get(async_client, f"/api/v1/library/files/{theirs.id}/image?token={token}")
    assert foreign.status_code == 404, "another user's picture was served to a read_own token"


@pytest.mark.asyncio
async def test_a_token_without_a_library_read_right_is_forbidden(async_client, db_session, tmp_path):
    jwt, _ = await _make_user(async_client, username="img_no_lib", permissions=["archives:read_own"])
    file_id = (await _image_row(db_session, tmp_path)).id
    token = await _mint(async_client, jwt)

    got = await _raw_get(async_client, f"/api/v1/library/files/{file_id}/image?token={token}")
    assert got.status_code == 403
