"""API routes for library file notes (gh#3)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.library import _ensure_library_file_visible
from backend.app.core.auth import get_current_user_optional, require_ownership_permission, require_permission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote
from backend.app.models.user import User
from backend.app.schemas.library_file_note import (
    LibraryFileNoteCreate,
    LibraryFileNoteResponse,
    LibraryFileNoteUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library-notes"])

# Notes are part of a file: every route resolves the file through the library's
# own visibility rule first — a file the caller cannot see, or one in the trash,
# is the same 404 as a missing one (G11 U2). The route's own permission
# (``library:read`` / ``library:notes_write``) is asked on top.
_SEES_FILE = Depends(require_ownership_permission(Permission.LIBRARY_READ_ALL, Permission.LIBRARY_READ_OWN))


async def _visible_file(db: AsyncSession, file_id: int, auth_result: tuple[User | None, bool]) -> LibraryFile:
    user, can_read_all = auth_result
    row = (await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one_or_none()
    return _ensure_library_file_visible(row, user, can_read_all)


def _may_change(note: LibraryFileNote, file: LibraryFile, current_user: User | None) -> bool:
    """May this caller edit or delete this note? The one rule of both doors and of ``can_edit``.

    A note is its author's. ⚠️ A note NOBODY wrote — m231 moved a file's old
    note text into the thread without an author when the file had no owner, and
    deleting a user leaves their notes authorless — would otherwise stay for as
    long as the file does. Such a note is the file's: whoever may edit the file
    (``library:update_all``, or its owner under ``library:update_own``) may
    change it. Another user's note stays that user's, whatever the caller holds.
    """
    if current_user is None or note.user_id == current_user.id:
        return True
    if note.user_id is not None:
        return False
    if current_user.has_permission(Permission.LIBRARY_UPDATE_ALL.value):
        return True
    return file.created_by_id == current_user.id and current_user.has_permission(Permission.LIBRARY_UPDATE_OWN.value)


def _to_response(
    note: LibraryFileNote, username: str | None, current_user: User | None, file: LibraryFile
) -> LibraryFileNoteResponse:
    """Build a response with `can_edit` denormalised for the frontend (see :func:`_may_change`)."""
    can_edit = _may_change(note, file, current_user)
    return LibraryFileNoteResponse(
        id=note.id,
        library_file_id=note.library_file_id,
        user_id=note.user_id,
        user_username=username,
        body=note.body,
        created_at=note.created_at,
        updated_at=note.updated_at,
        can_edit=can_edit,
    )


async def _file_notes_count(db: AsyncSession, file_id: int) -> int:
    result = await db.execute(select(func.count(LibraryFileNote.id)).where(LibraryFileNote.library_file_id == file_id))
    return int(result.scalar() or 0)


@router.get("/files/{file_id}/notes", response_model=list[LibraryFileNoteResponse])
async def list_file_notes(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    _: User | None = Depends(require_permission(Permission.LIBRARY_READ)),
    auth_result: tuple[User | None, bool] = _SEES_FILE,
):
    """List notes for a library file (newest first)."""
    file = await _visible_file(db, file_id, auth_result)

    rows = await db.execute(
        select(LibraryFileNote, User.username)
        .outerjoin(User, LibraryFileNote.user_id == User.id)
        .where(LibraryFileNote.library_file_id == file_id)
        .order_by(LibraryFileNote.created_at.desc(), LibraryFileNote.id.desc())
    )
    return [_to_response(note, username, current_user, file) for note, username in rows.all()]


@router.post("/files/{file_id}/notes", response_model=LibraryFileNoteResponse)
async def create_file_note(
    file_id: int,
    body: LibraryFileNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    _: User | None = Depends(require_permission(Permission.LIBRARY_NOTES_WRITE)),
    auth_result: tuple[User | None, bool] = _SEES_FILE,
):
    """Create a note on a library file."""
    file = await _visible_file(db, file_id, auth_result)

    note = LibraryFileNote(
        library_file_id=file_id,
        user_id=current_user.id if current_user else None,
        body=body.body,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    count = await _file_notes_count(db, file_id)
    await ws_manager.send_library_file_notes_changed(file_id, count)

    username = current_user.username if current_user else None
    return _to_response(note, username, current_user, file)


async def _load_note_with_username(db: AsyncSession, note_id: int) -> tuple[LibraryFileNote, str | None] | None:
    rows = await db.execute(
        select(LibraryFileNote, User.username)
        .outerjoin(User, LibraryFileNote.user_id == User.id)
        .where(LibraryFileNote.id == note_id)
    )
    row = rows.first()
    if row is None:
        return None
    return row[0], row[1]


@router.patch("/notes/{note_id}", response_model=LibraryFileNoteResponse)
async def update_file_note(
    note_id: int,
    body: LibraryFileNoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    _: User | None = Depends(require_permission(Permission.LIBRARY_NOTES_WRITE)),
    auth_result: tuple[User | None, bool] = _SEES_FILE,
):
    """Update a note. Its author can edit it; a note nobody wrote, whoever may edit the file.

    When auth is disabled (current_user is None), no ownership check applies -
    the install is single-user-trusted by definition.
    """
    loaded = await _load_note_with_username(db, note_id)
    if loaded is None:
        raise HTTPException(404, "Note not found")
    note, username = loaded
    file = await _visible_file(db, note.library_file_id, auth_result)

    if not _may_change(note, file, current_user):
        raise HTTPException(403, "Cannot edit another user's note")

    note.body = body.body
    await db.commit()
    await db.refresh(note)

    count = await _file_notes_count(db, note.library_file_id)
    await ws_manager.send_library_file_notes_changed(note.library_file_id, count)

    return _to_response(note, username, current_user, file)


@router.delete("/notes/{note_id}")
async def delete_file_note(
    note_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    _: User | None = Depends(require_permission(Permission.LIBRARY_NOTES_WRITE)),
    auth_result: tuple[User | None, bool] = _SEES_FILE,
):
    """Delete a note. Its author can delete it; a note nobody wrote, whoever may edit the file."""
    loaded = await _load_note_with_username(db, note_id)
    if loaded is None:
        raise HTTPException(404, "Note not found")
    note, _username = loaded
    file = await _visible_file(db, note.library_file_id, auth_result)

    if not _may_change(note, file, current_user):
        raise HTTPException(403, "Cannot delete another user's note")

    file_id = note.library_file_id
    await db.delete(note)
    await db.commit()

    count = await _file_notes_count(db, file_id)
    await ws_manager.send_library_file_notes_changed(file_id, count)

    return {"success": True}
