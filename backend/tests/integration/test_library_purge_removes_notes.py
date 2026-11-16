"""A purged library file takes its notes with it — on SQLite too.

``library_file_notes.library_file_id`` is ``ON DELETE CASCADE``, which PostgreSQL
honours and SQLite never runs. A purge that deletes file rows with a Core bulk
DELETE goes past the ORM cascade as well, so the notes stayed behind — and
``library_files`` hands the next file ``max(id) + 1``, which can be the purged
file's id: the stranger would open with somebody else's notes.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote
from backend.app.models.user import User


async def _file_with_notes(db, **fields) -> int:
    row = LibraryFile(filename="noted.3mf", file_path="/tmp/noted.3mf", file_size=10, file_type="3mf", **fields)
    db.add(row)
    await db.flush()
    db.add_all([LibraryFileNote(library_file_id=row.id, body=body, user_id=None) for body in ("first", "second")])
    await db.commit()
    return row.id


async def _notes_of(db, file_id: int) -> int:
    db.expire_all()
    return (
        await db.execute(select(func.count(LibraryFileNote.id)).where(LibraryFileNote.library_file_id == file_id))
    ).scalar_one()


async def _gone(db, file_id: int) -> bool:
    return (await db.execute(select(LibraryFile.id).where(LibraryFile.id == file_id))).scalar_one_or_none() is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_retention_sweeper_removes_the_notes_of_the_files_it_purges(db_session):
    from backend.app.services.library_trash import library_trash_service

    await library_trash_service.set_retention_days(db_session, 30)
    file_id = await _file_with_notes(db_session, deleted_at=datetime.now(timezone.utc) - timedelta(days=40))
    kept_id = await _file_with_notes(db_session)

    assert await library_trash_service._sweep(db_session) >= 1

    assert await _gone(db_session, file_id)
    assert await _notes_of(db_session, file_id) == 0
    assert await _notes_of(db_session, kept_id) == 2, "another file's notes are not the purge's"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_a_user_with_their_files_removes_those_files_notes(db_session):
    from backend.app.api.routes.users import delete_user
    from backend.tests.auth_helpers import mint_test_authority

    owner = User(username="leaver")
    admin = User(username="admin", role="admin")
    db_session.add_all([owner, admin])
    await db_session.flush()
    file_id = await _file_with_notes(db_session, created_by_id=owner.id)
    kept_id = await _file_with_notes(db_session)

    admin._auth_capture = await mint_test_authority(db_session, username=admin.username)
    await delete_user(user_id=owner.id, delete_items=True, _admin=admin, current_user=admin, db=db_session)

    assert await _gone(db_session, file_id)
    assert await _notes_of(db_session, file_id) == 0
    assert await _notes_of(db_session, kept_id) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_immediate_hard_delete_removes_the_notes(db_session):
    from backend.app.services.library_trash import library_trash_service

    file_id = await _file_with_notes(db_session, deleted_at=datetime.now(timezone.utc))
    await library_trash_service.hard_delete_now(db_session, await db_session.get(LibraryFile, file_id))

    assert await _gone(db_session, file_id)
    assert await _notes_of(db_session, file_id) == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purging_an_external_file_removes_the_notes(db_session):
    from backend.app.services.library_trash import library_trash_service

    file_id = await _file_with_notes(db_session, is_external=True)
    trashed = await library_trash_service.trash_or_purge(db_session, await db_session.get(LibraryFile, file_id))
    await db_session.commit()

    assert trashed is False
    assert await _gone(db_session, file_id)
    assert await _notes_of(db_session, file_id) == 0
