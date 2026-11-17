"""The registry of archive videos — its only writer (G10 C, m237).

``archive_media_assets`` holds one row per video published to an archive.
``print_archives.timelapse_path`` remains what readers follow; it is the
projection of the archive's ``primary`` row.

⚠️ **Only this module writes the table** (``tests/unit/
test_archive_media_has_one_writer.py``). The publication seam that keeps the
row and the pointer in one transaction, the revision check and the source claim
build on it; this first part carries what the table needs to exist safely: the
rows go when their archive goes, and a reader can ask what an archive has.

⚠️ **A row is bound to the archive's ``recovery_identity``, not to its id.**
SQLite hands a deleted archive's id to the next one, so every read here compares
the identity and a row left under a reused id answers nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive_media_asset import ROLE_PRIMARY, ArchiveMediaAsset

logger = logging.getLogger(__name__)

#: Ids per statement: SQLite's bound-parameter ceiling is 999 on older builds.
_CHUNK = 500


async def primary_of(db: AsyncSession, archive_id: int, archive_identity: str | None) -> ArchiveMediaAsset | None:
    """The archive's primary video row, or ``None`` — also when the row belongs to an earlier archive of that id."""
    if not archive_identity:
        return None
    return (
        await db.execute(
            select(ArchiveMediaAsset).where(
                ArchiveMediaAsset.archive_id == archive_id,
                ArchiveMediaAsset.archive_identity == archive_identity,
                ArchiveMediaAsset.role == ROLE_PRIMARY,
            )
        )
    ).scalar_one_or_none()


async def assets_of(db: AsyncSession, archive_id: int, archive_identity: str | None) -> list[ArchiveMediaAsset]:
    """Every video row of this archive, oldest revision first."""
    if not archive_identity:
        return []
    return list(
        (
            await db.execute(
                select(ArchiveMediaAsset)
                .where(
                    ArchiveMediaAsset.archive_id == archive_id,
                    ArchiveMediaAsset.archive_identity == archive_identity,
                )
                .order_by(ArchiveMediaAsset.revision, ArchiveMediaAsset.id)
            )
        )
        .scalars()
        .all()
    )


async def forget_archives(db: AsyncSession, archive_ids: Iterable[int]) -> int:
    """Remove every registry row of archives that are being hard-deleted. Never commits.

    Called by every door that deletes ``print_archives`` rows, before the
    delete: SQLite runs no FK action, and a row left behind would sit under an
    id the next archive takes. By id on purpose, whatever identity the rows
    carry — a row of an EARLIER archive of that id is exactly what must not
    survive either. The files are the archive's own to remove; this touches the
    table only.
    """
    ids = sorted({int(i) for i in archive_ids if i is not None})
    removed = 0
    for start in range(0, len(ids), _CHUNK):
        result = await db.execute(
            delete(ArchiveMediaAsset).where(ArchiveMediaAsset.archive_id.in_(ids[start : start + _CHUNK]))
        )
        removed += result.rowcount or 0
    return removed
