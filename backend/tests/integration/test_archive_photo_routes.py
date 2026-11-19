"""The manual photo doors of an archive change its list under the archive's write scope (G10 G)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
async def shot(async_client, db_session, printer_factory, archive_factory, monkeypatch, tmp_path):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
    printer = await printer_factory()
    archive = await archive_factory(printer.id, print_name="Lamp", file_path="archive/1/lamp/lamp.gcode.3mf")
    return SimpleNamespace(
        id=archive.id,
        photos=tmp_path / "archive" / "1" / "lamp" / "photos",
        url=f"/api/v1/archives/{archive.id}/photos",
    )


async def _listed(db, archive_id: int) -> list[str]:
    return list((await db.execute(select(PrintArchive.photos).where(PrintArchive.id == archive_id))).scalar_one() or [])


async def _upload(client, shot, name: str = "result.jpg"):
    return await client.post(shot.url, files={"file": (name, JPEG, "image/jpeg")})


async def test_an_upload_is_written_and_listed(async_client, shot, db_session):
    response = await _upload(async_client, shot)
    assert response.status_code == 200, response.text
    name = response.json()["filename"]
    assert (shot.photos / name).read_bytes() == JPEG
    assert response.json()["photos"] == [name]
    assert await _listed(db_session, shot.id) == [name]


async def test_two_uploads_are_both_kept(async_client, shot, db_session):
    first = (await _upload(async_client, shot)).json()["filename"]
    second = (await _upload(async_client, shot, "other.jpg")).json()["filename"]
    assert await _listed(db_session, shot.id) == [first, second]


async def test_an_upload_whose_archive_became_another_leaves_no_file(async_client, shot, monkeypatch):
    """The number is the same, the print is not: nothing is listed and nothing stays on disk."""
    from backend.app.services import archive_write_scope

    async def another(_db, _archive_id):
        return SimpleNamespace(recovery_identity="another-print", photos=None)

    monkeypatch.setattr(archive_write_scope, "load_active_archive_for_write", another)
    response = await _upload(async_client, shot)
    assert response.status_code == 404
    assert not shot.photos.exists() or list(shot.photos.iterdir()) == []


async def test_an_upload_that_cannot_be_listed_leaves_no_file(async_client, shot, monkeypatch):
    from backend.app.services import archive_write_scope

    async def broken(_db, _archive_id):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(archive_write_scope, "load_active_archive_for_write", broken)
    with pytest.raises(RuntimeError):
        await _upload(async_client, shot)
    assert list(shot.photos.iterdir()) == []


async def test_a_delete_removes_the_row_and_then_the_file(async_client, shot, db_session):
    name = (await _upload(async_client, shot)).json()["filename"]
    response = await async_client.delete(f"{shot.url}/{name}")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "deleted", "photos": None}
    assert not (shot.photos / name).exists()
    assert await _listed(db_session, shot.id) == []


async def test_a_delete_that_cannot_change_the_row_keeps_the_file(async_client, shot, db_session, monkeypatch):
    """The file used to go first: a failure after it left a photo the archive listed and could not show."""
    from backend.app.services import archive_write_scope

    name = (await _upload(async_client, shot)).json()["filename"]

    async def broken(_db, _archive_id):
        raise RuntimeError("the database went away")

    with monkeypatch.context() as patched:
        patched.setattr(archive_write_scope, "load_active_archive_for_write", broken)
        with pytest.raises(RuntimeError):
            await async_client.delete(f"{shot.url}/{name}")
    assert (shot.photos / name).is_file()
    assert await _listed(db_session, shot.id) == [name]


async def test_deleting_a_photo_that_is_not_listed_is_404(async_client, shot):
    response = await async_client.delete(f"{shot.url}/nothing.jpg")
    assert response.status_code == 404
