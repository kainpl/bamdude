"""API routes for a library file's photos (m233).

Two questions, asked here; everything else is ``services/library_file_photos``:

* **may the caller SEE the file** — the library's own rule
  (``_ensure_library_file_visible``): a hidden, trashed or missing file is the
  same 404, for the list, the pictures and every write alike;
* **may the caller EDIT it** — their own file, or any with
  ``library:update_all`` — for adding, captioning and deleting.

The two pictures take the MEDIA token (an ``<img>`` cannot send a header) and
then the same visibility rule; the list and the writes stay behind the header.
A photo is always addressed through its file: an id under another file's path
is a 404, so the file's rule cannot be walked round.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.library import _LIBRARY_MEDIA_READ, _ensure_library_file_visible
from backend.app.core.auth import acting_user, require_ownership_permission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_photo import LibraryFilePhoto
from backend.app.models.user import User
from backend.app.schemas.library_file_photo import LibraryFilePhotoResponse, LibraryFilePhotoUpdate
from backend.app.services import library_file_photos as photos
from backend.app.services.preview_artifacts import disk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library-photos"])

_READ = Depends(require_ownership_permission(Permission.LIBRARY_READ_ALL, Permission.LIBRARY_READ_OWN))
_UPDATE = Depends(require_ownership_permission(Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_UPDATE_OWN))

# A photo never changes and its id is never reused, so a browser may keep it —
# privately: what a user may see is theirs, not a shared cache's.
_CACHE = {"Cache-Control": "private, max-age=3600"}


async def _visible_file(db: AsyncSession, file_id: int, read: tuple[User | None, bool]) -> LibraryFile:
    user, can_read_all = read
    result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    return _ensure_library_file_visible(result.scalar_one_or_none(), user, can_read_all)


async def _editable_file(
    db: AsyncSession, file_id: int, read: tuple[User | None, bool], update: tuple[User | None, bool]
) -> LibraryFile:
    file = await _visible_file(db, file_id, read)
    user, can_update_all = update
    if not can_update_all and (user is None or file.created_by_id != user.id):
        raise HTTPException(status_code=403, detail="You can only update your own files")
    return file


async def _photo_of(db: AsyncSession, file: LibraryFile, photo_id: int) -> LibraryFilePhoto:
    photo = await photos.get_photo(db, file.id, photo_id)
    if photo is None:
        raise HTTPException(404, "Photo not found")
    return photo


@router.get("/files/{file_id}/photos", response_model=list[LibraryFilePhotoResponse])
async def list_file_photos(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    read: tuple[User | None, bool] = _READ,
):
    """A file's photos, oldest first."""
    file = await _visible_file(db, file_id, read)
    return await photos.list_photos(db, file.id)


@router.post("/files/{file_id}/photos", response_model=LibraryFilePhotoResponse, status_code=201)
async def add_file_photo(
    file_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    read: tuple[User | None, bool] = _READ,
    update: tuple[User | None, bool] = _UPDATE,
):
    """Attach one photo (PNG, JPEG or WebP). It is saved again without its metadata."""
    library_file = await _editable_file(db, file_id, read, update)
    # One byte past the cap is enough to know the upload is over it.
    content = await file.read(photos.MAX_PHOTO_BYTES + 1)
    author = await acting_user(request, db, update[0])
    return await photos.add_photo(
        db,
        library_file,
        content,
        file.filename,
        author.id if author else None,
        author.username if author else None,
    )


@router.patch("/files/{file_id}/photos/{photo_id}", response_model=LibraryFilePhotoResponse)
async def update_file_photo(
    file_id: int,
    photo_id: int,
    data: LibraryFilePhotoUpdate,
    db: AsyncSession = Depends(get_db),
    read: tuple[User | None, bool] = _READ,
    update: tuple[User | None, bool] = _UPDATE,
):
    """Set or clear a photo's caption."""
    library_file = await _editable_file(db, file_id, read, update)
    photo = await _photo_of(db, library_file, photo_id)
    return await photos.set_caption(db, photo, data.caption)


@router.delete("/files/{file_id}/photos/{photo_id}")
async def delete_file_photo(
    file_id: int,
    photo_id: int,
    db: AsyncSession = Depends(get_db),
    read: tuple[User | None, bool] = _READ,
    update: tuple[User | None, bool] = _UPDATE,
):
    """Remove a photo and its files."""
    library_file = await _editable_file(db, file_id, read, update)
    photo = await _photo_of(db, library_file, photo_id)
    await photos.delete_photo(db, photo)
    return {"success": True}


async def _serve(db: AsyncSession, file_id: int, photo_id: int, auth: tuple[User | None, bool], *, small: bool):
    file = await _visible_file(db, file_id, auth)
    photo = await _photo_of(db, file, photo_id)
    path = photos.photo_file(photo, small=small)
    if path is None or not await disk(path.is_file):
        raise HTTPException(404, "Photo not found")
    media_type = photos.THUMBNAIL_MEDIA_TYPE if small else photo.content_type
    return FileResponse(str(path), media_type=media_type, headers=_CACHE)


@router.get("/files/{file_id}/photos/{photo_id}/image")
async def get_file_photo(
    file_id: int,
    photo_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = _LIBRARY_MEDIA_READ,
):
    """A photo, full size — media token or headers, by the file's own visibility."""
    return await _serve(db, file_id, photo_id, auth_result, small=False)


@router.get("/files/{file_id}/photos/{photo_id}/thumbnail")
async def get_file_photo_thumbnail(
    file_id: int,
    photo_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = _LIBRARY_MEDIA_READ,
):
    """A photo's small copy for the gallery — media token or headers."""
    return await _serve(db, file_id, photo_id, auth_result, small=True)
