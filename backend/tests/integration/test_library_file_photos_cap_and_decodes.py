"""The photo cap is decided under the file row's lock, and decodes are few at once (review B-M8).

Counting and then inserting without a lock lets parallel uploads pass the cap:
each counts before the other has inserted. And one allowed picture decodes to
hundreds of megabytes, so the number decoded at once is bounded too.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from datetime import datetime

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_photo import LibraryFilePhoto
from backend.app.services import library_file_photos as photos

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16), (200, 30, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


async def _file(db) -> LibraryFile:
    row = LibraryFile(filename="lamp.3mf", file_path="library/files/lamp.3mf", file_size=10, file_type="3mf")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    db.expunge(row)
    return row


async def _held(db, file_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(LibraryFilePhoto).where(LibraryFilePhoto.library_file_id == file_id)
        )
        or 0
    )


async def test_an_upload_that_lands_while_this_one_decodes_counts_against_the_cap(db_session, monkeypatch):
    """The other upload's row arrives between this one's first look and its insert."""
    monkeypatch.setattr(photos, "MAX_PHOTOS_PER_FILE", 1)
    file = await _file(db_session)
    real_disk = photos.disk

    async def disk_while_another_upload_lands(function, *args, **kwargs):
        result = await real_disk(function, *args, **kwargs)
        if function is photos._encode_and_write:
            db_session.add(
                LibraryFilePhoto(
                    library_file_id=file.id,
                    stored_name="e" * 32 + ".jpg",
                    content_type="image/jpeg",
                    file_size=1,
                    width=1,
                    height=1,
                    created_at=datetime(2026, 1, 1),
                )
            )
            await db_session.flush()
        return result

    monkeypatch.setattr(photos, "disk", disk_while_another_upload_lands)

    with pytest.raises(HTTPException) as refused:
        await photos.add_photo(db_session, file, _jpeg(), "a.jpg", None, None)

    assert refused.value.status_code == 409
    # Refused before its row: nothing of this upload stays on disk.
    folder = photos.photos_dir(file.id)
    assert not folder.exists() or list(folder.iterdir()) == []


async def test_the_cap_is_counted_after_the_file_row_is_locked(db_session, monkeypatch):
    file = await _file(db_session)
    order: list[str] = []
    real_lock, real_scalar = photos.take_write_lock, db_session.scalar

    async def lock(db, table, row_id):
        order.append(f"lock {table.name} {row_id}")
        await real_lock(db, table, row_id)

    async def scalar(statement, *args, **kwargs):
        if "count(" in str(statement).lower():
            order.append("count")
        return await real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(photos, "take_write_lock", lock)
    monkeypatch.setattr(db_session, "scalar", scalar)

    await photos.add_photo(db_session, file, _jpeg(), "a.jpg", None, None)

    assert f"lock library_files {file.id}" in order
    assert order.index(f"lock library_files {file.id}") < len(order) - 1 - order[::-1].index("count")
    assert await _held(db_session, file.id) == 1


async def test_on_postgresql_the_lock_is_the_file_row_for_update():
    """SQLite compiles ``FOR UPDATE`` to nothing; the statement PostgreSQL gets is checked as text."""
    compiled = str(photos._lock_file_stmt(7).compile(dialect=postgresql.dialect()))
    assert "FROM library_files" in compiled and compiled.rstrip().endswith("FOR UPDATE")


async def test_a_file_purged_while_its_photo_was_decoded_takes_no_photo(db_session):
    file = await _file(db_session)
    await db_session.execute(LibraryFile.__table__.delete().where(LibraryFile.id == file.id))
    await db_session.commit()

    with pytest.raises(HTTPException) as refused:
        await photos.add_photo(db_session, file, _jpeg(), "a.jpg", None, None)

    assert refused.value.status_code == 404
    assert await _held(db_session, file.id) == 0
    folder = photos.photos_dir(file.id)
    assert not folder.exists() or list(folder.iterdir()) == []


async def test_only_a_few_photos_are_decoded_at_once(tmp_path, monkeypatch):
    guard = threading.Lock()
    running = 0
    most = 0

    def slow_decode(content, directory, stem):
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        time.sleep(0.15)
        with guard:
            running -= 1
        return "jpg", 1, 1, 1

    monkeypatch.setattr(photos, "_encode_and_write", slow_decode)

    await asyncio.gather(*(photos._decoded(b"x", tmp_path, f"{n:032x}") for n in range(8)))

    assert most == photos.PHOTO_DECODES_AT_ONCE == 2
