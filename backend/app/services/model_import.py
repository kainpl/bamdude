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
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
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
        "authority": authority,
        "credential_snapshots": credential_snapshots,
        "publication_check": publication_check,
    }
    if service.streams_downloads:
        # Outside library storage: nothing is written there except by the ingest door itself.
        handle, staged_name = tempfile.mkstemp(prefix="bamdude-model-import-", suffix=".part")
        os.close(handle)
        staged = Path(staged_name)
        try:
            await service.download_to(info, staged)
            filename = filename_for(info)
            validate_print_file_upload(filename, await asyncio.to_thread(_head, staged))
            result = await save_3mf_bytes_to_library(db, content=None, staged=staged, filename=filename, **common)
        finally:
            await asyncio.to_thread(staged.unlink, True)
    else:
        download = await service.download(info)
        result = await save_3mf_bytes_to_library(db, content=download.file_bytes, filename=filename_for(info), **common)
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
