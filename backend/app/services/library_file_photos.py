"""The one writer of a library file's photos (m233).

``library_file_photos`` and the folder ``<library>/photos/<library_file_id>/``
are written here and nowhere else (``tests/unit/test_library_file_photos_have_one_writer.py``).
Routes decide WHO may ask — reading follows the file's visibility, changing its
edit rule; this module decides what is stored and what is removed.

What the rules are:

* **A photo is never stored as sent.** It is decoded, turned upright and saved
  again in its own format with only its colour profile — the same encoder part
  photos go through (``part_images._saved_again``) — so EXIF (a phone's GPS
  position, the camera's serial), XMP and comments never reach another reader.
  PNG, JPEG and WebP only; a small copy for the gallery is made from the same
  pixels. Decoding runs in a thread.
* **A name on disk is the writer's.** ``<32 hex>.<ext>``; the client's file name
  is kept for display as a bare base name and is never part of a path.
* **Rows are the authority, the folder is named by the file's id.** A file in
  the folder that no row names is nothing — never served — so a folder left
  behind cannot hand its pictures to the next file that gets the id. Even so
  every place a library file is purged calls :func:`forget_files` (SQLite runs
  no FK actions), which removes the rows and the files they name.
* **Files follow the commit.** A removal waits for the transaction that removed
  the row: :func:`forget_files` runs inside somebody else's purge and only
  notes the files on the session — they go after the ROOT commit, a rolled-back
  savepoint takes its notes with it, and a rollback or a commit that raised
  removes nothing. ⚠️ Only the files the removed rows named are removed, never
  the folder wholesale: SQLite hands a purged file's id — the folder's name — to
  the next file, and the removal runs after the commit.
* **All or nothing, and never a row without its picture.** :func:`add_photo`
  writes the files, inserts the row and commits; whatever stops it before the
  commit is SENT — a refusal, a disk error, a cancellation, a failed flush —
  removes the files it wrote. When the commit itself raises, its outcome is
  read back: no row, the files go; a row, the photo stands; no answer, the
  files stay (a file no row names is found by the pruner — a row without its
  picture is a broken photo). Every file operation runs through
  ``preview_artifacts.disk``, which a cancelled caller still waits for.
* **They belong to the library row.** Trashing the file keeps them (restore
  brings them back); they are not copied to an archive, and another row with
  the same bytes has none.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image, ImageOps
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.core.database import take_write_lock
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_photo import LibraryFilePhoto
from backend.app.services.part_images import PHOTO_FORMATS, _saved_again
from backend.app.services.preview_artifacts import disk
from backend.app.utils.safe_path import safe_join_under

logger = logging.getLogger(__name__)

#: The folder under the library's root. Only this module names it.
PHOTOS_DIRECTORY = "photos"

#: An upload larger than this is refused unread; so is a photo that is larger
#: once it has been saved again.
MAX_PHOTO_BYTES = 20 * 1024 * 1024
#: Checked from the header, before a pixel is decoded.
MAX_PHOTO_PIXELS = 40_000_000
MAX_PHOTOS_PER_FILE = 30
#: Photos decoded at once, server-wide. One allowed picture (40 MP) is hundreds
#: of megabytes while it is decoded, turned upright and saved again; the rest wait.
PHOTO_DECODES_AT_ONCE = 2
CAPTION_MAX_LENGTH = 500
#: The longer side of the gallery's small copy.
THUMBNAIL_SIDE = 480

# The decoders an upload is opened with, so no other Pillow plugin ever parses
# its bytes. ``MPO`` (most phone JPEGs) is answered by the JPEG decoder.
_DECODERS = ("PNG", "JPEG", "WEBP")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
THUMBNAIL_MEDIA_TYPE = "image/jpeg"
STORED_NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|webp)\Z")
_ORIGINAL_NAME_MAX = 255


# -- where things are ------------------------------------------------------------


def photos_root() -> Path:
    return Path(settings.library_dir) / PHOTOS_DIRECTORY


def photos_dir(library_file_id: int) -> Path:
    """The folder of one library file's photos. The id is an integer or nothing."""
    return safe_join_under(photos_root(), str(int(library_file_id)), http=False)


def thumbnail_name(stored_name: str) -> str:
    return f"{stored_name.split('.', 1)[0]}.sm.jpg"


def photo_file(photo: LibraryFilePhoto, *, small: bool = False) -> Path | None:
    """The file behind a row, or ``None`` for a row whose name is not one of ours."""
    if not STORED_NAME.fullmatch(photo.stored_name or ""):
        return None
    name = thumbnail_name(photo.stored_name) if small else photo.stored_name
    return safe_join_under(photos_dir(photo.library_file_id), name, http=False)


def _files_of(directory: Path, stem: str) -> list[Path]:
    """Every file one photo can consist of, whatever got written."""
    return [directory / f"{stem}.{ext}" for ext in MEDIA_TYPES] + [directory / f"{stem}.sm.jpg"]


# -- the disk (always in a thread, through ``disk``) -------------------------------


def _unlink_all(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Library photo file %s was not removed: %s", path.name, type(exc).__name__)


def _remove_folder_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass  # not empty, or already gone


def _remove_forgotten(paths: list[Path]) -> None:
    _unlink_all(paths)
    for folder in sorted({path.parent for path in paths}):
        _remove_folder_if_empty(folder)


# -- after the commit ---------------------------------------------------------------
#
# The shape of ``part_images``' own seam (its event-order notes apply): removals
# are noted on the session, a savepoint remembers how many there were when it
# opened, and only the ROOT commit carries them out.

_STATE = "library_file_photos_after_commit"
_tasks: set[asyncio.Task] = set()
_SHUTDOWN_GRACE_SECONDS = 5.0


class _Pending:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.marks: dict = {}  # savepoint -> how many paths were noted when it opened


def _remove_after_commit(db, paths: list[Path]) -> None:
    info = getattr(db, "sync_session", db).info
    state = info.get(_STATE)
    if state is None:
        state = info[_STATE] = _Pending()
    state.paths.extend(paths)


@event.listens_for(Session, "after_transaction_create")
def _savepoint_opened(session, transaction) -> None:
    state = session.info.get(_STATE)
    if transaction.nested and state is not None:
        state.marks[transaction] = len(state.paths)


@event.listens_for(Session, "after_soft_rollback")
def _savepoint_rolled_back(session, previous_transaction) -> None:
    state = session.info.get(_STATE)
    if not previous_transaction.nested or state is None:
        return
    # A savepoint opened before the first note has no mark: everything noted since is its own.
    del state.paths[state.marks.pop(previous_transaction, 0) :]


@event.listens_for(Session, "after_commit")
def _committed(session) -> None:
    if session.in_nested_transaction():
        return  # a savepoint released: its notes stay with the transaction around it
    state = session.info.pop(_STATE, None)
    if state is None or not state.paths:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Library photo files were not removed after their purge: no running event loop")
        return
    task = loop.create_task(disk(_remove_forgotten, list(state.paths)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@event.listens_for(Session, "after_transaction_end")
def _root_ended(session, transaction) -> None:
    if transaction.parent is None:
        # Ended without a root commit — a rollback, a close, a commit that raised: nothing is removed.
        session.info.pop(_STATE, None)


async def drain() -> None:
    """Wait for every removal already started (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def shutdown() -> None:
    """Lifespan end: let removals that started finish briefly, then cancel the rest."""
    try:
        await asyncio.wait_for(drain(), _SHUTDOWN_GRACE_SECONDS)
    except TimeoutError:
        for task in list(_tasks):
            task.cancel()
        await asyncio.gather(*list(_tasks), return_exceptions=True)


class _Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _encode(content: bytes) -> tuple[str, bytes, bytes, int, int]:
    """``(ext, photo, small, width, height)`` — the photo saved again and its small copy.

    Nothing is decoded before the header says the picture is one of ours and
    within the pixel cap: a decompression bomb is refused on its declared size.
    """
    try:
        with Image.open(io.BytesIO(content), formats=_DECODERS) as probe:
            fmt, (width, height) = probe.format, probe.size
    except Image.DecompressionBombError as exc:
        raise _Refused("pixels") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise _Refused("format") from exc
    ext = PHOTO_FORMATS.get(fmt or "")
    if ext is None:
        raise _Refused("format")
    if width * height > MAX_PHOTO_PIXELS:
        raise _Refused("pixels")
    try:
        with Image.open(io.BytesIO(content), formats=_DECODERS) as image:
            image.load()  # a truncated file is refused before anything is written
            upright = ImageOps.exif_transpose(image)
            profile = upright.info.get("icc_profile")
            size = upright.size
            photo = _saved_again(image, upright, ext)
            small = upright
            small.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE))
            if small.mode in ("RGBA", "LA", "PA") or "transparency" in small.info:
                # A JPEG has no transparency: the small copy sits on white.
                rgba = small.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                small = flat
            elif small.mode != "RGB":
                small = small.convert("RGB")
            small.info = {}
            buffer = io.BytesIO()
            small.save(buffer, "JPEG", quality=82, optimize=True, **({"icc_profile": profile} if profile else {}))
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise _Refused("format") from exc
    if len(photo) > MAX_PHOTO_BYTES:
        raise _Refused("bytes")
    return ext, photo, buffer.getvalue(), size[0], size[1]


def _write_files(directory: Path, stem: str, ext: str, photo: bytes, small: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.{ext}").write_bytes(photo)
    (directory / f"{stem}.sm.jpg").write_bytes(small)


def _encode_and_write(content: bytes, directory: Path, stem: str) -> tuple[str, int, int, int]:
    ext, photo, small, width, height = _encode(content)
    _write_files(directory, stem, ext, photo, small)
    return ext, len(photo), width, height


_decode_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _slots() -> asyncio.Semaphore:
    """The decode slots of the running loop (a semaphore belongs to the loop it first waited on)."""
    global _decode_slots
    loop = asyncio.get_running_loop()
    if _decode_slots is None or _decode_slots[0] is not loop:
        _decode_slots = (loop, asyncio.Semaphore(PHOTO_DECODES_AT_ONCE))
    return _decode_slots[1]


async def _decoded(content: bytes, directory: Path, stem: str) -> tuple[str, int, int, int]:
    """:func:`_encode_and_write` in a thread, at most :data:`PHOTO_DECODES_AT_ONCE` at a time."""
    async with _slots():
        return await disk(_encode_and_write, content, directory, stem)


def _lock_file_stmt(library_file_id: int):
    return select(LibraryFile.id).where(LibraryFile.id == library_file_id).with_for_update()


async def _lock_file(db, library_file_id: int) -> bool:
    """The file row, locked for a read-decide-write; ``False`` when it is gone.

    SQLite's write lock first (``take_write_lock`` — its dialect emits no
    ``FOR UPDATE``), then the row lock PostgreSQL honours.
    """
    await take_write_lock(db, LibraryFile.__table__, library_file_id)
    return await db.scalar(_lock_file_stmt(library_file_id)) is not None


# -- reads -----------------------------------------------------------------------------


async def list_photos(db, library_file_id: int) -> list[LibraryFilePhoto]:
    rows = await db.execute(
        select(LibraryFilePhoto)
        .where(LibraryFilePhoto.library_file_id == library_file_id)
        .order_by(LibraryFilePhoto.created_at, LibraryFilePhoto.id)
    )
    return list(rows.scalars())


async def get_photo(db, library_file_id: int, photo_id: int) -> LibraryFilePhoto | None:
    """The photo, only as a photo OF THIS FILE — the file is what the caller was allowed to see."""
    return await db.scalar(
        select(LibraryFilePhoto).where(
            LibraryFilePhoto.id == photo_id, LibraryFilePhoto.library_file_id == library_file_id
        )
    )


async def counts(db, library_file_ids: Iterable[int]) -> dict[int, int]:
    ids = list(library_file_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(LibraryFilePhoto.library_file_id, func.count(LibraryFilePhoto.id))
        .where(LibraryFilePhoto.library_file_id.in_(ids))
        .group_by(LibraryFilePhoto.library_file_id)
    )
    return {file_id: int(count) for file_id, count in rows.all()}


# -- writes ----------------------------------------------------------------------------


def _display_name(name: str | None) -> str | None:
    """The client's name for the file, for display: its last component, printable, capped."""
    text = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:_ORIGINAL_NAME_MAX] or None


def clean_caption(caption: str | None) -> str | None:
    text = " ".join((caption or "").split())
    if len(text) > CAPTION_MAX_LENGTH:
        raise HTTPException(422, f"A caption may be at most {CAPTION_MAX_LENGTH} characters")
    return text or None


async def _commit(db) -> None:
    await db.commit()


async def _read_back(db, library_file_id: int, stored_name: str) -> LibraryFilePhoto | None:
    """The row a commit of unknown outcome was writing, if it is there."""
    return await db.scalar(
        select(LibraryFilePhoto).where(
            LibraryFilePhoto.library_file_id == library_file_id, LibraryFilePhoto.stored_name == stored_name
        )
    )


async def add_photo(
    db, file: LibraryFile, content: bytes, original_name: str | None, user_id: int | None, user_name: str | None
) -> LibraryFilePhoto:
    """Store one photo of ``file`` and commit it. All or nothing."""
    library_file_id = file.id
    if not content:
        raise HTTPException(422, "The photo must be a PNG, JPEG or WebP picture")
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(413, f"A photo may be at most {MAX_PHOTO_BYTES} bytes")
    full = HTTPException(409, f"A file may have at most {MAX_PHOTOS_PER_FILE} photos")
    count = (
        select(func.count()).select_from(LibraryFilePhoto).where(LibraryFilePhoto.library_file_id == library_file_id)
    )
    # A first look, so a full file is refused before anything is decoded. It decides nothing.
    if int(await db.scalar(count) or 0) >= MAX_PHOTOS_PER_FILE:
        raise full

    directory = photos_dir(library_file_id)
    stem = uuid4().hex
    files = _files_of(directory, stem)
    sent = False
    try:
        try:
            ext, stored_bytes, width, height = await _decoded(content, directory, stem)
        except _Refused as refused:
            if refused.reason == "bytes":
                raise HTTPException(
                    413, f"Saved again without its metadata, the photo would be larger than {MAX_PHOTO_BYTES} bytes"
                ) from refused
            if refused.reason == "pixels":
                raise HTTPException(422, f"The photo may have at most {MAX_PHOTO_PIXELS} pixels") from refused
            raise HTTPException(422, "The photo must be a PNG, JPEG or WebP picture") from refused
        # ⚠️ The cap is decided HERE, under the file row's lock and after the slow
        # decode: counted before it, parallel uploads each saw room for one more.
        # The lock is held only for the count, the insert and the commit.
        if not await _lock_file(db, library_file_id):
            raise HTTPException(404, "File not found")  # purged while its photo was decoded
        if int(await db.scalar(count) or 0) >= MAX_PHOTOS_PER_FILE:
            raise full
        photo = LibraryFilePhoto(
            library_file_id=library_file_id,
            stored_name=f"{stem}.{ext}",
            original_name=_display_name(original_name),
            content_type=MEDIA_TYPES[ext],
            file_size=stored_bytes,
            width=width,
            height=height,
            caption=None,
            uploaded_by_id=user_id,
            uploaded_by_name=(user_name or None) and user_name[:150],
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(photo)
        await db.flush()  # a row the database refuses is refused here, before the commit
        stored_name = photo.stored_name
        sent = True
        await _commit(db)
    except BaseException as error:
        landed: LibraryFilePhoto | None = None
        known = not sent  # nothing was committed before the commit was asked for
        try:
            await db.rollback()
            if sent:
                # ⚠️ The commit raised: it may have landed all the same (a connection
                # lost after the server committed). Ask, instead of deleting the
                # files of a row that may be durable.
                landed = await _read_back(db, library_file_id, stored_name)
                known = True
        except BaseException:
            logger.warning(
                "Photo of library file %s: the outcome of its commit is not known (%s); its files are kept",
                library_file_id,
                type(error).__name__,
            )
        if landed is None:
            if known:
                await disk(_unlink_all, files)
                await disk(_remove_folder_if_empty, directory)
            raise
        if not isinstance(error, Exception):
            raise  # a cancellation is still a cancellation; the photo stands
        logger.warning(
            "Photo of library file %s was committed although its commit raised %s",
            library_file_id,
            type(error).__name__,
        )
        photo = landed
    await db.refresh(photo)
    await _announce(library_file_id)
    return photo


async def set_caption(db, photo: LibraryFilePhoto, caption: str | None) -> LibraryFilePhoto:
    photo.caption = clean_caption(caption)
    await _commit(db)
    await db.refresh(photo)
    await _announce(photo.library_file_id)
    return photo


async def delete_photo(db, photo: LibraryFilePhoto) -> None:
    """Remove one photo: the row first, its files once the row is gone for good."""
    library_file_id = photo.library_file_id
    paths = [p for p in (photo_file(photo), photo_file(photo, small=True)) if p is not None]
    await db.delete(photo)
    await _commit(db)
    await disk(_unlink_all, paths)
    await disk(_remove_folder_if_empty, photos_dir(library_file_id))
    await _announce(library_file_id)


async def forget_files(db, library_file_ids: Iterable[int]) -> None:
    """These library files are being purged: their photo rows go, and their files after the commit.

    Called in the purging transaction and does not commit. ⚠️ Nothing leaves the
    disk here: the caller's transaction may still roll back, and the rows would
    come back without their pictures. The files the rows named are noted on the
    session and removed once its root commit has landed (see the module's
    "Files follow the commit").
    """
    ids = sorted({int(i) for i in library_file_ids if i is not None})
    if not ids:
        return
    rows = (
        await db.execute(
            select(LibraryFilePhoto.library_file_id, LibraryFilePhoto.stored_name).where(
                LibraryFilePhoto.library_file_id.in_(ids)
            )
        )
    ).all()
    if not rows:
        return
    paths: list[Path] = []
    for library_file_id, stored_name in rows:
        if STORED_NAME.fullmatch(stored_name or ""):
            folder = photos_dir(library_file_id)
            paths += [folder / stored_name, folder / thumbnail_name(stored_name)]
    await db.execute(
        delete(LibraryFilePhoto).where(LibraryFilePhoto.library_file_id.in_(sorted({row[0] for row in rows})))
    )
    _remove_after_commit(db, paths)


async def detach_user(db, user_id: int) -> None:
    """A user is being deleted: their photos stay, under the name snapshot."""
    await db.execute(
        update(LibraryFilePhoto).where(LibraryFilePhoto.uploaded_by_id == user_id).values(uploaded_by_id=None)
    )


async def _announce(library_file_id: int) -> None:
    """Tell open clients the file's details changed. Never fails the write it follows."""
    from backend.app.core.websocket import ws_manager

    try:
        await ws_manager.send_library_file_details_changed(library_file_id)
    except Exception:
        logger.exception("Photo change of library file %s was not announced", library_file_id)
