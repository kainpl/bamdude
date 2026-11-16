"""Photos on a library file (``library_file_photos``, m233).

An operator attaches photos to a file — how the print came out, the assembled
thing. They belong to that library ROW: reading follows the file's visibility
(a trashed file hides them), adding / captioning / deleting follows the file's
edit rule. A photo is never stored as sent: it is turned upright and saved
again without its metadata, so a phone's GPS position never reaches another
reader. Purging the file removes the rows and the folder.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import func, select

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_photo import LibraryFilePhoto
from backend.app.services import library_file_photos as photos
from backend.tests.integration.test_media_token import _make_user, _mint, _raw_get

pytestmark = pytest.mark.integration


def _jpeg(size=(64, 32), *, gps: bool = False, orientation: int | None = None, color=(200, 30, 30)) -> bytes:
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    if gps:
        exif[0x010F] = "SecretCameraMaker"
        exif[0x8825] = {1: "N", 2: (50.0, 27.0, 1.5), 3: "E", 4: (30.0, 31.0, 2.5)}
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif.tobytes(), comment=b"private-comment")
    return buffer.getvalue()


def _png(size=(40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (10, 200, 10, 128)).save(buffer, "PNG")
    return buffer.getvalue()


async def _row(db, *, name: str = "lamp.3mf", owner: int | None = None, **extra) -> LibraryFile:
    row = LibraryFile(
        filename=name,
        file_path=f"library/files/{name}",
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


async def _upload(client, file_id: int, content: bytes, name: str = "photo.jpg", headers: dict | None = None):
    return await client.post(
        f"/api/v1/library/files/{file_id}/photos",
        files={"file": (name, content, "application/octet-stream")},
        headers=headers or {},
    )


async def _count(db, file_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(LibraryFilePhoto).where(LibraryFilePhoto.library_file_id == file_id)
        )
        or 0
    )


def _folder(file_id: int) -> Path:
    return photos.photos_dir(file_id)


# -- the round trip -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_photo_is_added_listed_served_captioned_and_deleted(async_client, db_session):
    file_id = (await _row(db_session)).id

    added = await _upload(async_client, file_id, _jpeg(), "IMG_0001.JPG")
    assert added.status_code == 201, added.text
    photo = added.json()
    assert photo["original_name"] == "IMG_0001.JPG"
    assert photo["content_type"] == "image/jpeg"
    assert (photo["width"], photo["height"]) == (64, 32)
    assert photo["caption"] is None
    assert photo["uploaded_by_name"]
    assert "stored_name" not in photo, "the name on disk is the writer's business"

    listed = (await async_client.get(f"/api/v1/library/files/{file_id}/photos")).json()
    assert [p["id"] for p in listed] == [photo["id"]]

    base = f"/api/v1/library/files/{file_id}/photos/{photo['id']}"
    full = await async_client.get(f"{base}/image")
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(full.content)).size == (64, 32)
    small = await async_client.get(f"{base}/thumbnail")
    assert small.status_code == 200 and small.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(small.content)).size) <= photos.THUMBNAIL_SIDE

    captioned = await async_client.patch(base, json={"caption": "  First print, PETG  "})
    assert captioned.status_code == 200, captioned.text
    assert captioned.json()["caption"] == "First print, PETG"
    assert (await async_client.patch(base, json={"caption": ""})).json()["caption"] is None
    assert (await async_client.patch(base, json={"caption": "x" * 501})).status_code == 422

    # The file's list entry and detail count it — the card's mark.
    listing = (await async_client.get("/api/v1/library/files")).json()
    items = listing["items"] if isinstance(listing, dict) else listing
    assert next(i for i in items if i["id"] == file_id)["photos_count"] == 1

    on_disk = sorted(p.name for p in _folder(file_id).iterdir())
    assert len(on_disk) == 2, on_disk

    assert (await async_client.delete(base)).status_code == 200
    assert (await async_client.get(f"{base}/image")).status_code == 404
    assert await _count(db_session, file_id) == 0
    assert not _folder(file_id).exists() or list(_folder(file_id).iterdir()) == []
    assert (await async_client.delete(base)).status_code == 404


@pytest.mark.asyncio
async def test_a_png_keeps_its_format_and_its_transparency(async_client, db_session):
    file_id = (await _row(db_session)).id
    photo = (await _upload(async_client, file_id, _png(), "cut.png")).json()
    assert photo["content_type"] == "image/png"
    full = await async_client.get(f"/api/v1/library/files/{file_id}/photos/{photo['id']}/image")
    served = Image.open(io.BytesIO(full.content))
    assert served.format == "PNG" and served.mode == "RGBA"


# -- what is stored is not what was sent ------------------------------------------


@pytest.mark.asyncio
async def test_gps_and_every_other_tag_are_gone_from_what_is_served(async_client, db_session):
    """The upload carries a GPS position, the camera's maker and a comment; neither
    the picture nor its thumbnail may — and the orientation is in the pixels."""
    sent = _jpeg((64, 32), gps=True, orientation=6)
    assert b"SecretCameraMaker" in sent and b"private-comment" in sent
    assert Image.open(io.BytesIO(sent)).getexif().get_ifd(0x8825), "the fixture must really carry GPS"
    file_id = (await _row(db_session)).id

    photo = (await _upload(async_client, file_id, sent, "geo.jpg")).json()

    # Orientation 6 = "rotate 90° to show": the stored picture is already upright.
    assert (photo["width"], photo["height"]) == (32, 64)
    for kind in ("image", "thumbnail"):
        got = await async_client.get(f"/api/v1/library/files/{file_id}/photos/{photo['id']}/{kind}")
        assert got.status_code == 200
        assert b"SecretCameraMaker" not in got.content, kind
        assert b"private-comment" not in got.content, kind
        assert b"Exif" not in got.content, kind
        served = Image.open(io.BytesIO(got.content))
        assert dict(served.getexif()) == {}, kind
        assert not served.getexif().get_ifd(0x8825), kind
        assert served.size[0] < served.size[1], f"{kind} is not upright"
    for path in _folder(file_id).iterdir():
        assert b"SecretCameraMaker" not in path.read_bytes(), path.name


@pytest.mark.asyncio
async def test_the_clients_file_name_never_becomes_a_path(async_client, db_session, tmp_path):
    from backend.app.core.config import settings

    file_id = (await _row(db_session)).id
    before = {p for p in Path(settings.base_dir).rglob("*") if p.is_file()}

    for hostile in ("../../../evil.jpg", "..\\..\\evil.jpg", "/etc/cron.d/evil.jpg", "a/b/c.jpg", "nul\x00.jpg"):
        added = await _upload(async_client, file_id, _jpeg(), hostile)
        assert added.status_code == 201, (hostile, added.text)
        assert "/" not in added.json()["original_name"] and "\\" not in added.json()["original_name"]
        assert "\x00" not in added.json()["original_name"]

    written = {p for p in Path(settings.base_dir).rglob("*") if p.is_file()} - before
    folder = _folder(file_id).resolve()
    assert written, "nothing was written at all"
    assert all(p.resolve().parent == folder for p in written), sorted(str(p) for p in written)
    assert not list(Path(settings.base_dir).rglob("evil*"))
    assert not (tmp_path.parent / "evil.jpg").exists()


# -- refusals ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,content",
    [
        ("notes.txt", b"just text"),
        ("fake.jpg", b"\xff\xd8\xff\xe0 not really a jpeg"),
        ("model.3mf", b"PK\x03\x04" + b"0" * 64),
        ("vector.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
        ("empty.jpg", b""),
    ],
)
async def test_what_is_not_a_picture_is_refused_and_leaves_nothing(async_client, db_session, name, content):
    file_id = (await _row(db_session)).id

    got = await _upload(async_client, file_id, content, name)

    assert got.status_code == 422, f"{name}: {got.status_code} {got.text}"
    assert isinstance(got.json()["detail"], str)
    assert await _count(db_session, file_id) == 0
    assert not _folder(file_id).exists() or list(_folder(file_id).iterdir()) == []


@pytest.mark.asyncio
async def test_a_gif_or_a_bmp_is_not_a_photo(async_client, db_session):
    file_id = (await _row(db_session)).id
    for fmt, name in (("GIF", "a.gif"), ("BMP", "a.bmp"), ("TIFF", "a.tiff")):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, fmt)
        assert (await _upload(async_client, file_id, buffer.getvalue(), name)).status_code == 422, fmt
    assert await _count(db_session, file_id) == 0


@pytest.mark.asyncio
async def test_a_photo_over_the_byte_cap_is_refused(async_client, db_session, monkeypatch):
    monkeypatch.setattr(photos, "MAX_PHOTO_BYTES", 2000)
    file_id = (await _row(db_session)).id
    big = _jpeg((400, 400)) + b"\x00" * 3000

    got = await _upload(async_client, file_id, big)

    assert got.status_code == 413, got.text
    assert await _count(db_session, file_id) == 0
    assert not _folder(file_id).exists() or list(_folder(file_id).iterdir()) == []


@pytest.mark.asyncio
async def test_a_photo_with_too_many_pixels_is_refused_before_it_is_decoded(async_client, db_session, monkeypatch):
    monkeypatch.setattr(photos, "MAX_PHOTO_PIXELS", 10_000)
    file_id = (await _row(db_session)).id

    got = await _upload(async_client, file_id, _jpeg((200, 100)))

    assert got.status_code == 422, got.text
    assert await _count(db_session, file_id) == 0


@pytest.mark.asyncio
async def test_a_file_takes_only_so_many_photos(async_client, db_session, monkeypatch):
    monkeypatch.setattr(photos, "MAX_PHOTOS_PER_FILE", 2)
    file_id = (await _row(db_session)).id
    assert (await _upload(async_client, file_id, _jpeg())).status_code == 201
    assert (await _upload(async_client, file_id, _jpeg())).status_code == 201

    third = await _upload(async_client, file_id, _jpeg())

    assert third.status_code == 409, third.text
    assert await _count(db_session, file_id) == 2
    assert len(list(_folder(file_id).iterdir())) == 4


# -- who may read, who may write ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reader_of_own_files_neither_sees_nor_changes_another_users_photos(async_client, db_session):
    jwt_a, uid_a = await _make_user(
        async_client, username="ph_own_a", permissions=["library:read_own", "library:update_own"]
    )
    _, uid_b = await _make_user(async_client, username="ph_own_b", permissions=["library:read_own"])
    mine = await _row(db_session, name="mine.3mf", owner=uid_a)
    theirs = await _row(db_session, name="theirs.3mf", owner=uid_b)
    mine_id, theirs_id = mine.id, theirs.id
    their_photo = (await _upload(async_client, theirs_id, _jpeg())).json()["id"]
    base = f"/api/v1/library/files/{theirs_id}/photos"

    assert (await _upload(async_client, mine_id, _jpeg(), headers=_as(jwt_a))).status_code == 201

    assert (await async_client.get(base, headers=_as(jwt_a))).status_code == 404
    assert (await _upload(async_client, theirs_id, _jpeg(), headers=_as(jwt_a))).status_code == 404
    assert (
        await async_client.patch(f"{base}/{their_photo}", headers=_as(jwt_a), json={"caption": "x"})
    ).status_code == 404
    assert (await async_client.delete(f"{base}/{their_photo}", headers=_as(jwt_a))).status_code == 404
    assert await _count(db_session, theirs_id) == 1

    token = await _mint(async_client, jwt_a)
    for kind in ("image", "thumbnail"):
        foreign = await _raw_get(async_client, f"{base}/{their_photo}/{kind}?token={token}")
        assert foreign.status_code == 404, f"another user's photo {kind} was served to a read_own token"
    # A photo id under the wrong file is no way round the file's rule.
    crossed = await _raw_get(async_client, f"/api/v1/library/files/{mine_id}/photos/{their_photo}/image?token={token}")
    assert crossed.status_code == 404


@pytest.mark.asyncio
async def test_seeing_every_file_is_not_the_right_to_change_its_photos(async_client, db_session):
    jwt, _ = await _make_user(
        async_client, username="ph_reader", permissions=["library:read_all", "library:update_own"]
    )
    _, uid_b = await _make_user(async_client, username="ph_owner", permissions=["library:read_own"])
    theirs_id = (await _row(db_session, name="theirs.3mf", owner=uid_b)).id
    photo = (await _upload(async_client, theirs_id, _jpeg())).json()["id"]
    base = f"/api/v1/library/files/{theirs_id}/photos"

    assert (await async_client.get(base, headers=_as(jwt))).status_code == 200
    assert (await _upload(async_client, theirs_id, _jpeg(), headers=_as(jwt))).status_code == 403
    assert (await async_client.patch(f"{base}/{photo}", headers=_as(jwt), json={"caption": "x"})).status_code == 403
    assert (await async_client.delete(f"{base}/{photo}", headers=_as(jwt))).status_code == 403
    assert await _count(db_session, theirs_id) == 1


@pytest.mark.asyncio
async def test_an_img_reaches_a_photo_with_only_a_media_token(committing_client, db_session):
    from backend.app.core.auth import create_media_token
    from backend.app.main import app
    from backend.tests.auth_helpers import mint_test_authority

    file_id = (await _row(db_session)).id
    photo = (await _upload(committing_client, file_id, _jpeg())).json()["id"]
    token = await create_media_token(authority=await mint_test_authority(username="test_admin"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        for kind in ("image", "thumbnail"):
            url = f"/api/v1/library/files/{file_id}/photos/{photo}/{kind}"
            assert (await anonymous.get(url)).status_code == 401, "no token is still no entry"
            assert (await anonymous.get(url, params={"token": token})).status_code == 200
        # The list and the writes stay behind the header.
        assert (
            await anonymous.get(f"/api/v1/library/files/{file_id}/photos", params={"token": token})
        ).status_code == 401


@pytest.mark.asyncio
async def test_a_trashed_file_hides_its_photos_and_keeps_them(async_client, db_session):
    row = await _row(db_session)
    file_id = row.id
    photo = (await _upload(async_client, file_id, _jpeg())).json()["id"]
    base = f"/api/v1/library/files/{file_id}/photos"

    assert (await async_client.delete(f"/api/v1/library/files/{file_id}")).json()["trashed"] is True

    assert (await async_client.get(base)).status_code == 404
    assert (await async_client.get(f"{base}/{photo}/image")).status_code == 404
    assert (await async_client.get(f"{base}/{photo}/thumbnail")).status_code == 404
    assert (await _upload(async_client, file_id, _jpeg())).status_code == 404
    assert (await async_client.delete(f"{base}/{photo}")).status_code == 404
    # In the trash, not gone: restoring the file brings its photos back.
    assert await _count(db_session, file_id) == 1
    assert len(list(_folder(file_id).iterdir())) == 2

    restored = await async_client.post(f"/api/v1/library/trash/{file_id}/restore")
    assert restored.status_code == 200, restored.text
    assert (await async_client.get(f"{base}/{photo}/image")).status_code == 200


# -- the file's end is its photos' end -------------------------------------------------


@pytest.mark.asyncio
async def test_purging_a_file_from_the_trash_removes_its_photos(async_client, db_session):
    doomed = (await _row(db_session, name="doomed.3mf")).id
    kept = (await _row(db_session, name="kept.3mf")).id
    await _upload(async_client, doomed, _jpeg())
    await _upload(async_client, doomed, _png(), "b.png")
    await _upload(async_client, kept, _jpeg())
    assert _folder(doomed).exists()

    await async_client.delete(f"/api/v1/library/files/{doomed}")
    purged = await async_client.delete(f"/api/v1/library/trash/{doomed}")
    assert purged.status_code in (200, 204), purged.text

    assert await _count(db_session, doomed) == 0
    assert not _folder(doomed).exists(), "the purged file's photo folder is still on disk"
    assert await _count(db_session, kept) == 1
    assert len(list(_folder(kept).iterdir())) == 2


@pytest.mark.asyncio
async def test_the_retention_sweep_removes_the_photos_of_what_it_purges(async_client, db_session):
    from datetime import timedelta

    from backend.app.services.library_trash import library_trash_service

    long_ago = datetime.now(timezone.utc) - timedelta(days=4000)
    old = (await _row(db_session, name="old.3mf")).id
    await _upload(async_client, old, _jpeg())
    row = await db_session.get(LibraryFile, old)
    row.deleted_at = long_ago
    await db_session.commit()

    assert await library_trash_service._sweep(db_session) == 1

    assert await _count(db_session, old) == 0
    assert not _folder(old).exists()


@pytest.mark.asyncio
async def test_an_external_file_deleted_outright_takes_its_photos_with_it(async_client, db_session):
    external = (await _row(db_session, name="ext.3mf", is_external=True)).id
    await _upload(async_client, external, _jpeg())

    deleted = await async_client.delete(f"/api/v1/library/files/{external}")
    assert deleted.status_code == 200 and deleted.json()["trashed"] is False

    assert await _count(db_session, external) == 0
    assert not _folder(external).exists()


@pytest.mark.asyncio
async def test_a_new_file_never_inherits_the_photos_of_the_id_it_reuses(async_client, db_session):
    """SQLite hands the next row ``max(id)+1``: after the newest file is purged its
    id — and its folder's name — goes to the next upload."""
    first = (await _row(db_session, name="first.3mf")).id
    await _upload(async_client, first, _jpeg())
    await async_client.delete(f"/api/v1/library/files/{first}")
    await async_client.delete(f"/api/v1/library/trash/{first}")

    second = (await _row(db_session, name="second.3mf")).id

    assert (await async_client.get(f"/api/v1/library/files/{second}/photos")).json() == []
    if second == first:  # SQLite; PostgreSQL never reuses the id
        assert not _folder(second).exists()


@pytest.mark.asyncio
async def test_two_rows_with_the_same_bytes_do_not_share_photos(async_client, db_session):
    one = (await _row(db_session, name="one.3mf", file_hash="a" * 64)).id
    two = (await _row(db_session, name="two.3mf", file_hash="a" * 64)).id
    await _upload(async_client, one, _jpeg())

    assert len((await async_client.get(f"/api/v1/library/files/{one}/photos")).json()) == 1
    assert (await async_client.get(f"/api/v1/library/files/{two}/photos")).json() == []


@pytest.mark.asyncio
async def test_a_write_that_fails_leaves_no_row_and_a_commit_that_fails_leaves_no_file(
    async_client, db_session, monkeypatch
):
    file_id = (await _row(db_session)).id

    def broken_write(directory, stem, ext, photo, small):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.{ext}").write_bytes(photo)
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(photos, "_write_files", broken_write)
        with pytest.raises(OSError):
            await _upload(async_client, file_id, _jpeg())
    assert await _count(db_session, file_id) == 0
    assert not _folder(file_id).exists() or list(_folder(file_id).iterdir()) == []

    async def broken_commit(db):
        raise RuntimeError("the database went away")

    with monkeypatch.context() as patch:
        patch.setattr(photos, "_commit", broken_commit)
        with pytest.raises(RuntimeError):
            await _upload(async_client, file_id, _jpeg())
    db_session.expire_all()
    assert await _count(db_session, file_id) == 0
    assert not _folder(file_id).exists() or list(_folder(file_id).iterdir()) == []
