"""Which library row a fresh arrival becomes.

Every path that brings a file into the library asks this module the same
question, so it cannot be answered two ways. It was answered two ways for a
long time: ``save_3mf_bytes_to_library`` returned ``was_existing: bool`` and
``store_library_upload`` returned ``duplicate_of: int | None`` — and both
created a new row regardless, so the answer was decoration. One of them even
said so in a comment: "the caller may use it to render an *exists already*
badge".

⚠️ **The contract is the feature.** Callers must carry on with
:attr:`IngestResult.file`. A caller that keeps its own row has silently opted
out of deduplication, and nothing at runtime will say so — which is why the
source guard in ``test_library_system_tags`` fails a construction site that does
not consult this module. That guard, and not a database constraint, is what
holds the invariant: a unique index would express it exactly and would refuse to
build on any install that already holds duplicates, turning a harmless
pre-existing mess into an install that never starts again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile

IngestOutcome = Literal["created", "deduped", "restored"]


@dataclass(slots=True)
class IngestResult:
    """What happened, and the row to use from here on.

    ``superseded_name`` is the name the caller was going to give the file. Kept
    so a surface can say "you sent X, we used Y" — a substitution the user
    cannot see reads as an upload that did nothing.
    """

    file: LibraryFile
    outcome: IngestOutcome
    superseded_name: str | None = None


async def _rows_with_hash(db: AsyncSession, content_hash: str) -> list[LibraryFile]:
    """Active rows carrying this content.

    ⚠️ Active only. A trashed sibling was deleted by the user and must not pin a
    fresh arrival to itself — the rule the upload paths have always applied.
    """
    result = await db.execute(LibraryFile.active().where(LibraryFile.file_hash == content_hash))
    return list(result.scalars().all())


def _file_present(row: LibraryFile) -> bool:
    """Whether the row's bytes are actually on disk.

    A path we cannot even resolve is a file we cannot claim is there, so every
    failure answers False and the caller supplies bytes.
    """
    from backend.app.api.routes.library import to_absolute_path

    try:
        absolute = to_absolute_path(row.file_path)
    except Exception:  # noqa: BLE001 - best-effort presence check, never fatal
        return False
    return bool(absolute) and Path(absolute).is_file()


async def find_reusable_row(db: AsyncSession, *, content_hash: str) -> tuple[LibraryFile, bool] | None:
    """The row a new arrival with this content should become, plus whether its
    bytes are on disk. ``None`` when the arrival must become a row of its own.

    Managed beats external — the managed row is the only one that cannot vanish
    when a mount goes away. Among equals, the lowest ``id`` wins: oldest,
    stable, and free to compute.

    ⚠️ An external row whose file is **absent** is not an answer. Its bytes live
    on somebody's mount, which may be unplugged or read-only; writing there is
    not ours to do, and a mount that is not currently attached is not an error
    to be corrected from here. A managed sibling behind it still counts.
    """
    rows = await _rows_with_hash(db, content_hash)
    if not rows:
        return None

    for candidate in sorted(rows, key=lambda r: (bool(r.is_external), r.id)):
        present = _file_present(candidate)
        if present or not candidate.is_external:
            return candidate, present

    return None


def external_hash_is_stale(row: LibraryFile, *, size: int, mtime: float | None) -> bool:
    """Whether a mounted file must be re-read to learn its hash.

    No new column is needed: ``file_size`` and ``fs_modified_at`` are already on
    the row (m129). Both matching what is on disk means the stored hash stands,
    so the first scan of a mount pays a full read and every scan after it
    re-reads only what changed.

    That is what retires the original "skip hashing external files for
    performance" decision — it was taken before the on-disk mtime was available
    to cache against, and the hole it bought was a whole mount outside
    deduplication.
    """
    if not row.file_hash:
        return True
    if row.file_size != size:
        return True
    return row.fs_modified_at != mtime
