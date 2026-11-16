"""A photo's files follow the outcome of the transaction that names them (review B-M3, B-M4).

``forget_files`` runs inside somebody else's purge; whether that purge lands is
decided later. A file removed before the commit cannot come back with the rows
when the transaction rolls back, so the removal waits for the commit — and a
commit whose outcome nobody knows removes nothing: a file no row names is found
by the pruner, a row without its picture is a broken photo.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import func, select

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_photo import LibraryFilePhoto
from backend.app.services import library_file_photos as photos

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16), (200, 30, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


class _Id:
    """The file's id, read once: an ORM row expires with every rollback in these tests."""

    def __init__(self, id_: int):
        self.id = id_


async def _file_with_a_photo(db) -> tuple[_Id, list]:
    row = LibraryFile(filename="lamp.3mf", file_path="library/files/lamp.3mf", file_size=10, file_type="3mf")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await photos.add_photo(db, row, _jpeg(), "a.jpg", None, None)
    on_disk = sorted(photos.photos_dir(row.id).iterdir())
    assert len(on_disk) == 2
    return _Id(row.id), on_disk


async def _rows(db, file_id: int) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count()).select_from(LibraryFilePhoto).where(LibraryFilePhoto.library_file_id == file_id)
        )
        or 0
    )


class TestForgettingAFilesPhotos:
    async def test_nothing_leaves_the_disk_before_the_purge_commits(self, db_session):
        row, on_disk = await _file_with_a_photo(db_session)

        await photos.forget_files(db_session, [row.id])
        await photos.drain()

        assert all(path.exists() for path in on_disk)

    async def test_a_purge_that_rolls_back_keeps_the_rows_and_the_pictures(self, db_session):
        row, on_disk = await _file_with_a_photo(db_session)

        await photos.forget_files(db_session, [row.id])
        await db_session.rollback()
        await photos.drain()

        assert await _rows(db_session, row.id) == 1
        assert all(path.exists() for path in on_disk)
        # …and a later, unrelated commit of the same session removes nothing either.
        await db_session.commit()
        await photos.drain()
        assert all(path.exists() for path in on_disk)

    async def test_a_purge_that_commits_removes_the_pictures_and_the_folder(self, db_session):
        row, on_disk = await _file_with_a_photo(db_session)

        await photos.forget_files(db_session, [row.id])
        await db_session.commit()
        await photos.drain()

        assert await _rows(db_session, row.id) == 0
        assert not any(path.exists() for path in on_disk)
        assert not photos.photos_dir(row.id).exists()

    async def test_a_savepoint_that_rolls_back_takes_its_removals_with_it(self, db_session):
        row, on_disk = await _file_with_a_photo(db_session)

        savepoint = await db_session.begin_nested()
        await photos.forget_files(db_session, [row.id])
        await savepoint.rollback()
        await db_session.commit()
        await photos.drain()

        assert await _rows(db_session, row.id) == 1
        assert all(path.exists() for path in on_disk)

    async def test_a_commit_of_unknown_outcome_removes_nothing(self, db_session, monkeypatch):
        row, on_disk = await _file_with_a_photo(db_session)
        await photos.forget_files(db_session, [row.id])

        def lost(*_args, **_kwargs):
            raise RuntimeError("the connection was lost during COMMIT")

        # The driver's commit raises: whether the server committed is not known.
        monkeypatch.setattr(db_session.sync_session, "commit", lost)
        with pytest.raises(RuntimeError):
            await db_session.commit()
        await photos.drain()

        assert all(path.exists() for path in on_disk)

    async def test_only_the_files_the_rows_named_are_removed(self, db_session):
        """SQLite hands a purged file's id to the next one: nothing of a newer row is touched."""
        row, on_disk = await _file_with_a_photo(db_session)
        stranger = photos.photos_dir(row.id) / ("f" * 32 + ".jpg")
        stranger.write_bytes(b"a newer row's picture")

        await photos.forget_files(db_session, [row.id])
        await db_session.commit()
        await photos.drain()

        assert not any(path.exists() for path in on_disk)
        assert stranger.exists()


class TestAddingAPhoto:
    async def _file(self, db) -> LibraryFile:
        row = LibraryFile(filename="vase.3mf", file_path="library/files/vase.3mf", file_size=10, file_type="3mf")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        db.expunge(row)  # stays readable across the rollbacks below
        return row

    async def test_a_commit_that_landed_but_raised_keeps_the_row_and_its_picture(self, db_session, monkeypatch):
        row = await self._file(db_session)

        async def landed_then_lost(db):
            await db.commit()
            raise RuntimeError("the connection was lost after COMMIT")

        monkeypatch.setattr(photos, "_commit", landed_then_lost)
        photo = await photos.add_photo(db_session, row, _jpeg(), "a.jpg", None, None)

        assert photos.photo_file(photo).exists() and photos.photo_file(photo, small=True).exists()
        assert await _rows(db_session, row.id) == 1

    async def test_a_commit_that_certainly_failed_leaves_no_row_and_no_file(self, db_session, monkeypatch):
        row = await self._file(db_session)

        async def never_sent(db):
            raise RuntimeError("the database went away")

        monkeypatch.setattr(photos, "_commit", never_sent)
        with pytest.raises(RuntimeError):
            await photos.add_photo(db_session, row, _jpeg(), "a.jpg", None, None)

        assert await _rows(db_session, row.id) == 0
        folder = photos.photos_dir(row.id)
        assert not folder.exists() or list(folder.iterdir()) == []

    async def test_when_nobody_can_say_whether_it_landed_the_files_stay(self, db_session, monkeypatch):
        row = await self._file(db_session)

        async def lost(db):
            raise RuntimeError("the connection was lost during COMMIT")

        async def unreadable(db, library_file_id, stored_name):
            raise RuntimeError("still no connection")

        monkeypatch.setattr(photos, "_commit", lost)
        monkeypatch.setattr(photos, "_read_back", unreadable)
        with pytest.raises(RuntimeError, match="during COMMIT"):
            await photos.add_photo(db_session, row, _jpeg(), "a.jpg", None, None)

        assert len(list(photos.photos_dir(row.id).iterdir())) == 2


class TestDeletingAPhoto:
    async def test_a_delete_whose_commit_failed_keeps_the_picture(self, db_session, monkeypatch):
        row, on_disk = await _file_with_a_photo(db_session)
        photo = (await photos.list_photos(db_session, row.id))[0]

        async def never_sent(db):
            raise RuntimeError("the database went away")

        monkeypatch.setattr(photos, "_commit", never_sent)
        with pytest.raises(RuntimeError):
            await photos.delete_photo(db_session, photo)
        await db_session.rollback()

        assert await _rows(db_session, row.id) == 1
        assert all(path.exists() for path in on_disk)
