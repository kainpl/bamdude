"""API routes for library file notes (gh#3)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import get_current_user_optional, require_permission
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


def _to_response(note: LibraryFileNote, username: str | None, current_user: User | None) -> LibraryFileNoteResponse:
    """Build a response with `can_edit` denormalised for the frontend.

    `can_edit` is True when:
    - auth is disabled (current_user is None) - UI shows controls; backend
      will still NULL-check at edit/delete time, but everything is open then.
    - the note belongs to the requesting user.
    """
    if current_user is None:
        can_edit = True
    else:
        can_edit = note.user_id == current_user.id
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
):
    """List notes for a library file (newest first)."""
    file_result = await db.execute(select(LibraryFile.id).where(LibraryFile.id == file_id))
    if file_result.scalar_one_or_none() is None:
        raise HTTPException(404, "Library file not found")

    rows = await db.execute(
        select(LibraryFileNote, User.username)
        .outerjoin(User, LibraryFileNote.user_id == User.id)
        .where(LibraryFileNote.library_file_id == file_id)
        .order_by(LibraryFileNote.created_at.desc(), LibraryFileNote.id.desc())
    )
    return [_to_response(note, username, current_user) for note, username in rows.all()]


@router.post("/files/{file_id}/notes", response_model=LibraryFileNoteResponse)
async def create_file_note(
    file_id: int,
    body: LibraryFileNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    _: User | None = Depends(require_permission(Permission.LIBRARY_NOTES_WRITE)),
):
    """Create a note on a library file."""
    file_result = await db.execute(select(LibraryFile.id).where(LibraryFile.id == file_id))
    if file_result.scalar_one_or_none() is None:
        raise HTTPException(404, "Library file not found")

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
    return _to_response(note, username, current_user)


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
):
    """Update a note. Only the original author can edit it.

    When auth is disabled (current_user is None), no ownership check applies -
    the install is single-user-trusted by definition.
    """
    loaded = await _load_note_with_username(db, note_id)
    if loaded is None:
        raise HTTPException(404, "Note not found")
    note, username = loaded

    if current_user is not None and note.user_id != current_user.id:
        raise HTTPException(403, "Cannot edit another user's note")

    note.body = body.body
    await db.commit()
    await db.refresh(note)

    count = await _file_notes_count(db, note.library_file_id)
    await ws_manager.send_library_file_notes_changed(note.library_file_id, count)

    return _to_response(note, username, current_user)


@router.delete("/notes/{note_id}")
async def delete_file_note(
    note_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
    _: User | None = Depends(require_permission(Permission.LIBRARY_NOTES_WRITE)),
):
    """Delete a note. Only the original author can delete it."""
    loaded = await _load_note_with_username(db, note_id)
    if loaded is None:
        raise HTTPException(404, "Note not found")
    note, _username = loaded

    if current_user is not None and note.user_id != current_user.id:
        raise HTTPException(403, "Cannot delete another user's note")

    file_id = note.library_file_id
    await db.delete(note)
    await db.commit()

    count = await _file_notes_count(db, file_id)
    await ws_manager.send_library_file_notes_changed(file_id, count)

    return {"success": True}
