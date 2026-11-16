"""m231 moves a library file's legacy ``notes`` text into its notes thread (G11, owner 2026-11-16).

The single text field (Bambuddy's) was still written and returned by the API,
and shown nowhere: the thread replaced it on screen long ago. Whatever an
installation kept there becomes a note of the thread; the field leaves the API.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m231_legacy_file_notes_to_thread import seed, split_note
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote
from backend.app.models.user import User

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CREATED = datetime(2024, 1, 2, 3, 4, 5)
UPDATED = datetime(2025, 6, 7, 8, 9, 10)


async def _file(db, name: str, notes: str | None, *, owner: int | None = None, updated=UPDATED) -> int:
    row = LibraryFile(
        filename=name,
        file_path=f"library/{name}",
        file_type="3mf",
        file_size=1,
        notes=notes,
        created_by_id=owner,
        created_at=CREATED,
        updated_at=updated,
    )
    db.add(row)
    await db.commit()
    return row.id


async def _run(test_engine):
    await seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))


async def _thread(db, file_id: int) -> list[LibraryFileNote]:
    """As the notes route orders it: newest first, then the higher id."""
    rows = await db.execute(
        select(LibraryFileNote)
        .where(LibraryFileNote.library_file_id == file_id)
        .order_by(LibraryFileNote.created_at.desc(), LibraryFileNote.id.desc())
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _legacy(db, file_id: int):
    return (await db.execute(text("SELECT notes FROM library_files WHERE id = :id"), {"id": file_id})).scalar_one()


async def test_the_text_becomes_one_note_by_the_files_owner_at_the_files_time(db_session, test_engine):
    owner = User(username="m231_owner", password_hash="x")
    db_session.add(owner)
    await db_session.commit()
    file_id = await _file(db_session, "lamp.3mf", "print at 0.2, brim on", owner=owner.id)

    await _run(test_engine)

    notes = await _thread(db_session, file_id)
    assert [(n.body, n.user_id, n.created_at, n.updated_at) for n in notes] == [
        ("print at 0.2, brim on", owner.id, UPDATED, UPDATED)
    ]
    assert await _legacy(db_session, file_id) is None


async def test_a_second_run_inserts_nothing(db_session, test_engine):
    file_id = await _file(db_session, "lamp.3mf", "keep the supports")

    await _run(test_engine)
    await _run(test_engine)

    assert [n.body for n in await _thread(db_session, file_id)] == ["keep the supports"]


async def test_no_owner_or_an_owner_who_is_gone_leaves_the_note_unsigned(db_session, test_engine):
    nobody = await _file(db_session, "a.3mf", "from nobody")
    ghost = await _file(db_session, "b.3mf", "from a deleted user", owner=987654)

    await _run(test_engine)

    assert [n.user_id for n in await _thread(db_session, nobody)] == [None]
    assert [n.user_id for n in await _thread(db_session, ghost)] == [None]


async def test_empty_and_blank_texts_make_no_note(db_session, test_engine):
    none = await _file(db_session, "a.3mf", None)
    empty = await _file(db_session, "b.3mf", "")
    blank = await _file(db_session, "c.3mf", "  \n ")

    await _run(test_engine)

    for file_id in (none, empty, blank):
        assert await _thread(db_session, file_id) == []


async def test_a_file_in_the_trash_keeps_its_text_too(db_session, test_engine):
    """It can be restored — and its text must come back with it."""
    file_id = await _file(db_session, "a.3mf", "trashed but mine")
    await db_session.execute(
        text("UPDATE library_files SET deleted_at = :at WHERE id = :id"), {"at": UPDATED, "id": file_id}
    )
    await db_session.commit()

    await _run(test_engine)

    assert [n.body for n in await _thread(db_session, file_id)] == ["trashed but mine"]


async def test_an_existing_thread_is_kept(db_session, test_engine):
    file_id = await _file(db_session, "a.3mf", "legacy")
    db_session.add(LibraryFileNote(library_file_id=file_id, user_id=None, body="already here"))
    await db_session.commit()

    await _run(test_engine)

    assert sorted(n.body for n in await _thread(db_session, file_id)) == ["already here", "legacy"]


async def test_a_text_longer_than_a_note_is_split_and_reads_in_order(db_session, test_engine):
    """The thread's body is 1000 characters (PostgreSQL enforces it): nothing is cut off."""
    words = " ".join(f"word{i:04d}" for i in range(400))  # 3599 characters
    file_id = await _file(db_session, "a.3mf", words)

    await _run(test_engine)

    notes = await _thread(db_session, file_id)
    assert len(notes) == 4
    assert all(len(n.body) <= 1000 for n in notes)
    assert " ".join(n.body for n in notes) == words


def test_split_note():
    assert split_note("short") == ["short"]
    assert split_note("  \n ") == []
    assert split_note("a" * 2500) == ["a" * 1000, "a" * 1000, "a" * 500]
    assert split_note("aaa bbb ccc", limit=7) == ["aaa bbb", "ccc"]
    assert split_note("line one\nline two", limit=10) == ["line one", "line two"]


# -- the field leaves the API --------------------------------------------------


async def test_the_api_no_longer_returns_or_writes_the_legacy_field(async_client, db_session):
    file_id = await _file(db_session, "a.3mf", None)
    url = f"/api/v1/library/files/{file_id}"

    assert "notes" not in (await async_client.get(url)).json()
    written = await async_client.put(url, json={"notes": "through the old door"})

    assert written.status_code == 200, written.text
    assert "notes" not in written.json()
    assert await _legacy(db_session, file_id) is None
