"""The provider-neutral import of one model file into the library.

Extracted from MakerWorld's ``import_instance`` so a second model source runs
the SAME steps instead of a copy of them: the destination folder (a named one,
else the provider's own top-level folder, created on first use), the
de-duplication by the provider's canonical key with the caller's read rights,
the download, and the library's ONE ingest door for automated sources
(``save_3mf_bytes_to_library``) with the caller's authority.

What stays the provider's — and so the route's — is named by parameters: the
file's name (``filename_for``), the credentials whose snapshot guards the
publication (``credential_snapshots`` / ``publication_check``), and the record
of where the file came from (``origin_of``). Provider errors are raised as
they are; each route maps them to its own HTTP answers.

A provider that ``streams_downloads`` writes the file to a staging file outside
library storage and the ingest door adopts it — the bytes are never held in
memory. A provider that does not (MakerWorld) hands its bytes over as before.

A streamed import is bounded (review MB-I4):

* it is staged in ``DATA_DIR/model-imports/`` — the app's own disk, normally the
  library's filesystem (adoption is then a rename), never the system temp
  directory, which is RAM under systemd's ``PrivateTmp`` on a tmpfs ``/tmp``;
* free space is asked for before the download starts and a disk that fills
  later is the same refusal — 507 ``storage_full``;
* at most ``MAX_CONCURRENT_IMPORTS`` stream at once in the installation; the
  rest wait their turn for ``IMPORT_QUEUE_WAIT_SECONDS``, then 503
  ``source_import_busy``;
* every path removes its staging file, and what a killed process left is swept
  at start-up: only ``model-import-*.part`` files of that directory, older
  than any import can run.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.services.library_ingest import IngestResult, read_access
from backend.app.services.model_providers.base import (
    ModelProvider,
    ProviderDownloadInfo,
    ProviderResourceRef,
    ProviderService,
)

logger = logging.getLogger(__name__)

STAGING_DIRNAME = "model-imports"
STAGING_PREFIX = "model-import-"
#: Streamed imports in flight per installation; the rest queue.
MAX_CONCURRENT_IMPORTS = 2
#: How long a queued import waits for its turn before it is refused (503).
IMPORT_QUEUE_WAIT_SECONDS = 120.0
#: Free space kept beyond the file's declared size (or asked for alone when it declares none).
SPACE_RESERVE_BYTES = 64 * 1024 * 1024
#: A staging file older than this belongs to no running import (a download may take 30 minutes).
STALE_STAGING_SECONDS = 6 * 3600.0

STORAGE_FULL = "The server has no space left for this file."
IMPORTS_BUSY = "Other imports are still running. Try again in a few minutes."

_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None
_active = 0
_waiting = 0


def staging_dir() -> Path:
    from backend.app.core.config import settings

    path = Path(settings.base_dir) / STAGING_DIRNAME  # SEC-PATH-OK: base and staging name are app-owned.
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_staging_file() -> Path:
    """A name for one import's bytes; the file itself is created by the download."""
    return staging_dir() / f"{STAGING_PREFIX}{uuid.uuid4().hex}.part"


def sweep_stale_staging(*, create: bool = True) -> int:
    """Remove what a dead process left: our own old ``.part`` files, nothing else. Returns the count."""
    from backend.app.core.config import settings

    directory = (
        staging_dir() if create else Path(settings.base_dir) / STAGING_DIRNAME
    )  # SEC-PATH-OK: no user path component, only the app-owned staging root.
    removed = 0
    limit = time.time() - STALE_STAGING_SECONDS
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            if (
                entry.name.startswith(STAGING_PREFIX)
                and entry.name.endswith(".part")
                and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < limit
            ):
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


#: A staging file a worker thread still holds (Windows refuses the unlink) is tried again this often.
REMOVE_ATTEMPTS = 10
REMOVE_PAUSE_SECONDS = 0.2


def remove_staged(path: Path, *, attempts: int | None = None, pause: float | None = None) -> bool:
    """Remove one staging file. Never raises: a file that stays held is the sweep's (its name says whose)."""
    attempts = REMOVE_ATTEMPTS if attempts is None else attempts
    pause = REMOVE_PAUSE_SECONDS if pause is None else pause
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(pause)
    logger.warning("A model import's staging file is still in use; a later sweep removes it")
    return False


@asynccontextmanager
async def staging(staged: Path) -> AsyncIterator[Path]:
    """The life of one staging file: gone when the block ends, however it ends.

    The removal is shielded and cannot raise — it runs while a refusal or a cancellation is on
    its way out, and must not replace it (on Windows the owned worker thread that is still
    copying the file refuses the unlink for a moment).
    """
    try:
        yield staged
    finally:
        await asyncio.shield(asyncio.to_thread(remove_staged, staged))


def storage_full() -> HTTPException:
    return HTTPException(status_code=507, detail={"error": "storage_full", "message": STORAGE_FULL})


def ensure_space(size: int | None) -> None:
    """Refuse (507) unless the staging disk has room for ``size`` bytes and the reserve."""
    try:
        free = shutil.disk_usage(staging_dir()).free
    except OSError:
        return  # a filesystem that will not say: the write itself answers
    if free < (size or 0) + SPACE_RESERVE_BYTES:
        raise storage_full()


def import_load() -> dict[str, int]:
    """Streamed imports right now: running, waiting for a slot, and the limit."""
    return {"active": _active, "waiting": _waiting, "limit": MAX_CONCURRENT_IMPORTS}


@asynccontextmanager
async def import_slot() -> AsyncIterator[None]:
    """One of the installation's import slots, waited for at most ``IMPORT_QUEUE_WAIT_SECONDS``."""
    global _slots, _slots_loop, _active, _waiting
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:
        _slots, _slots_loop, _active, _waiting = asyncio.Semaphore(MAX_CONCURRENT_IMPORTS), loop, 0, 0
    gate = _slots
    _waiting += 1
    try:
        async with asyncio.timeout(IMPORT_QUEUE_WAIT_SECONDS):
            await gate.acquire()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "source_import_busy", "message": IMPORTS_BUSY},
            headers={"Retry-After": "60"},
        ) from exc
    finally:
        _waiting -= 1
    _active += 1
    try:
        yield
    finally:
        _active -= 1
        gate.release()


@dataclass
class ModelImport:
    """What an import did. ``file`` may be a row this import did not create (``was_existing``)."""

    file: LibraryFile
    was_existing: bool
    ref: ProviderResourceRef
    source_url: str
    result: IngestResult | None = None


async def destination_folder(
    db: AsyncSession, provider: ModelProvider, folder_id: int | None, authority: Any
) -> LibraryFolder | None:
    """The folder an import lands in; ends its own short transaction.

    A named folder must exist and be writable. Without one: the provider's
    dedicated top-level folder, created on the first import — or the library
    root for a provider that names none.
    """
    from backend.app.services.domain_authority import guard

    await db.commit()
    await guard(db, authority, permissions=(Permission.LIBRARY_UPLOAD.value,))
    effective_folder: LibraryFolder | None
    if folder_id is not None:
        # Eager-load .products so save_3mf_bytes_to_library's
        # inherit_folder_products() doesn't trip async lazy-load.
        folder_q = await db.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder_id).options(selectinload(LibraryFolder.products))
        )
        target_folder = folder_q.scalar_one_or_none()
        if target_folder is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        if target_folder.is_external and target_folder.external_readonly:
            raise HTTPException(
                status_code=403,
                detail="Cannot import into a read-only external folder",
            )
        effective_folder = target_folder
    elif provider.default_folder_name is None:
        # A provider without a default folder imports into the library root
        # rather than minting a NULL-named folder.
        effective_folder = None
    else:
        # Default destination: the provider's dedicated top-level folder
        # ("MakerWorld"). Keeps imports out of the library root so power users
        # can still organise manually in subfolders, and auto-creates the
        # folder on the first import so users don't have to set it up.
        default_folder_q = await db.execute(
            select(LibraryFolder).where(
                LibraryFolder.name == provider.default_folder_name,
                LibraryFolder.parent_id.is_(None),
                LibraryFolder.is_external.is_(False),
            )
        )
        default_folder = default_folder_q.scalar_one_or_none()
        if default_folder is None:
            default_folder = LibraryFolder(name=provider.default_folder_name, parent_id=None)
            db.add(default_folder)
            await db.flush()
        effective_folder = default_folder
    await db.commit()
    return effective_folder


def _head(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(16)


async def _picture_mesh(db: AsyncSession, library_file: LibraryFile, authority: Any) -> None:
    """A new STL / OBJ gets the library's own rendered picture, as an upload does. Best effort."""
    from backend.app.api.routes import library as library_routes

    source_id = library_file.id
    extension = os.path.splitext(library_file.filename)[1].lower()
    if extension not in (".stl", ".obj") or (library_file.file_size or 0) < library_routes.MIN_USABLE_STL_BYTES:
        return
    try:
        source = library_routes.PreviewSource.capture(
            library_file, authority=authority, permission=Permission.LIBRARY_UPLOAD.value
        )
        await library_routes.generate_library_preview(db, source)
        await db.refresh(library_file)
    except Exception:
        logger.warning("Preview skipped for imported library file %s", source_id, exc_info=True)


async def import_resource(
    db: AsyncSession,
    *,
    provider: ModelProvider,
    service: ProviderService,
    ref: ProviderResourceRef,
    folder: LibraryFolder | None,
    authority: Any,
    fresh_authority: Callable[[], Awaitable[Any]],
    created_by_id: int | None,
    filename_for: Callable[[ProviderDownloadInfo], str],
    credential_snapshots: tuple = (),
    publication_check: Callable[[AsyncSession, Any], Awaitable[None]] | None = None,
    origin_of: Callable[[ProviderResourceRef], Awaitable[Any]] | None = None,
) -> ModelImport:
    """Download ``ref`` and put it in the library, or answer with the row that already holds it.

    De-duplicates by the provider's canonical key — an earlier import of the same
    resource is returned, if the caller may see it, and nothing is downloaded.
    Raises ``ProviderError`` for the provider's failures and ``HTTPException``
    for the library's.
    """
    from backend.app.api.routes.library import save_3mf_bytes_to_library, validate_print_file_upload
    from backend.app.services.cloud_credentials import guard_snapshot
    from backend.app.services.domain_authority import guard

    # Finding the concrete download (and, for a provider that picks one when none
    # was named, which sub-resource) lives in ``get_download``, which hands the
    # choice back in ``info.ref``.
    info = await service.get_download(ref)
    # The canonical key includes the sub-resource, so each plate / file gets its
    # own library entry (see ``ModelProvider.canonical_url``).
    source_url = provider.canonical_url(info.ref)

    # Dedupe check upfront so we don't burn bandwidth re-downloading.
    existing_q = await db.execute(
        LibraryFile.active().where(LibraryFile.source_url == source_url).order_by(LibraryFile.id).limit(1)
    )
    existing_row = existing_q.scalar_one_or_none()
    if existing_row is not None:
        await guard(db, authority, permissions=(Permission.LIBRARY_UPLOAD.value,))
        for credential in credential_snapshots:
            await guard_snapshot(db, credential)
        (await read_access(db, authority=await fresh_authority())).ensure(existing_row)
        await db.commit()
        return ModelImport(file=existing_row, was_existing=True, ref=info.ref, source_url=source_url)

    common = {
        "folder": folder,
        "created_by_id": created_by_id,
        "source_type": provider.source_type,
        "source_url": source_url,
        "credential_snapshots": credential_snapshots,
        "publication_check": publication_check,
    }
    if service.streams_downloads:
        async with import_slot():
            # Outside library storage: nothing is written there except by the ingest door itself.
            await asyncio.to_thread(sweep_stale_staging)  # what an earlier import could not remove
            await asyncio.to_thread(ensure_space, info.size)
            async with staging(await asyncio.to_thread(new_staging_file)) as staged:
                try:
                    await service.download_to(info, staged)
                    filename = filename_for(info)
                    validate_print_file_upload(filename, await asyncio.to_thread(_head, staged))
                    result = await save_3mf_bytes_to_library(
                        db, content=None, staged=staged, filename=filename, authority=authority, **common
                    )
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        raise storage_full() from exc
                    raise
    else:
        download = await service.download(info)
        result = await save_3mf_bytes_to_library(
            db, content=download.file_bytes, filename=filename_for(info), authority=authority, **common
        )
    # ⚠️ ``result.file`` may be a row this import did not create — either the
    # same source_url was imported before, or another row already holds these
    # exact bytes. Both mean the work that follows a new file was already done.
    library_file = result.file
    created = result.outcome == "created"
    if created:
        await _picture_mesh(db, library_file, authority)
    if created and origin_of is not None:
        # Where the file came from (m235). Never breaks the import: the file is saved.
        from backend.app.services import library_import_meta

        try:
            origin = await origin_of(info.ref)
            await guard(db, authority, permissions=(Permission.LIBRARY_UPLOAD.value,))
            current = await db.scalar(LibraryFile.active().where(LibraryFile.id == library_file.id))
            if current is not None and current.file_hash == library_file.file_hash:
                await library_import_meta.record(
                    db, library_file_id=library_file.id, source_type=provider.source_type, origin=origin
                )
            await db.commit()
        except Exception:
            logger.warning("Import origin not recorded for a library file", exc_info=True)
            await db.rollback()
            await db.refresh(library_file)
    return ModelImport(file=library_file, was_existing=not created, ref=info.ref, source_url=source_url, result=result)
