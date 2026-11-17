"""The one writer and reader of ``library_file_import_meta`` (m235).

``record`` is called by the model-import core for a file an import CREATED (a
deduplicated or restored row keeps whatever it had); ``forget_files`` runs
inside every purge of a library file, because SQLite runs no FK actions and the
next file may be given the id; ``for_file`` answers the File details dialog.
Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library_file_import_meta import LibraryFileImportMeta


@dataclass(frozen=True)
class ImportOrigin:
    """What a provider knows about the model a file came from. Every text is the source's own."""

    source_model_id: str
    source_file_id: str | None = None
    title: str | None = None
    creator: str | None = None
    license: str | None = None
    canonical_url: str | None = None


def _clip(value: str | None, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


async def record(db: AsyncSession, *, library_file_id: int, source_type: str, origin: ImportOrigin) -> None:
    """Write the origin of a file this import created; an existing row is left as it is."""
    existing = await db.scalar(
        select(LibraryFileImportMeta.id).where(LibraryFileImportMeta.library_file_id == library_file_id)
    )
    if existing is not None:
        return
    db.add(
        LibraryFileImportMeta(
            library_file_id=library_file_id,
            source_type=source_type[:32],
            source_model_id=origin.source_model_id[:128],
            source_file_id=_clip(origin.source_file_id, 128),
            title=_clip(origin.title, 500),
            creator=_clip(origin.creator, 255),
            license=_clip(origin.license, 128),
            canonical_url=_clip(origin.canonical_url, 1024),
            imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.flush()


async def for_file(db: AsyncSession, library_file_id: int) -> LibraryFileImportMeta | None:
    return await db.scalar(
        select(LibraryFileImportMeta).where(LibraryFileImportMeta.library_file_id == library_file_id)
    )


async def forget_files(db: AsyncSession, library_file_ids: Iterable[int]) -> None:
    """These library files are being purged: their import records go with them."""
    ids = sorted({int(i) for i in library_file_ids if i is not None})
    if ids:
        await db.execute(delete(LibraryFileImportMeta).where(LibraryFileImportMeta.library_file_id.in_(ids)))
