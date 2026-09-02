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
from datetime import datetime, timezone
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


async def trash_duplicate_rows(db: AsyncSession) -> tuple[int, int]:
    """Send byte-identical duplicates already in the library to the trash.

    Returns ``(groups, trashed)``. Called once per install, by m141.

    ⚠️ **Soft-delete only. Nothing is re-pointed, and that is the whole design.**
    A merge would have to reconcile four uniqueness constraints —
    ``library_file_makerworld_meta`` is 1:1 per file, tags and product links are
    unique pairs, and a plate row is unique per (product, file, plate), so
    merging means choosing which file a product's plate points at. Setting
    ``deleted_at`` leaves every foreign key intact: ``print_archives`` keeps its
    link, queue rows keep theirs, and the dedup lookup already ignores trashed
    rows, so the duplicate simply stops competing. It is also reversible, which
    is what makes doing this to somebody else's library acceptable at all.

    ⚠️ **Never route this through ``trash_or_purge``.** Its external branch
    *purges* — nulling ``PrintArchive.library_file_id`` and deleting queue rows
    on the way out — which is exactly the damage this avoids.

    Survivor: the row something points at. When every duplicate is referenced,
    the lowest ``id`` wins — oldest, stable, free to compute. That ordering
    matters because the trash sweeper eventually purges what nobody rescued, so
    it should take the row that was never referenced anyway.

    ⚠️ **Columns by name, and reference counts in bulk — both because a
    migration calls this.** An entity-wide ``select(LibraryFile)`` breaks mid-
    chain the moment a later migration adds a column, and a per-row COUNT would
    be seven queries per row: fine for a button, tens of thousands of queries at
    startup on a library that actually has duplicates.

    ⚠️ **The filing tables it counts differ by era, so it asks the database
    which ones are there.** m141 is frozen and runs at two different moments:
    on an existing install it runs BEFORE m162, when the legacy
    ``library_file_projects`` / ``project_print_plan_items`` still exist and
    ``product_files`` does not; on a fresh install ``create_all`` has already
    made the product tables and the legacy pair never existed at all. Importing
    either era's model would break the other half — the legacy models are gone
    from the tree, and the product ones are absent from the schema mid-upgrade.
    A reference the counter cannot see is a survivor it will not protect, so
    every table that is present is counted and every one that is not is skipped.
    """
    from sqlalchemy import bindparam, func, select, text, update

    from backend.app.migrations.helpers import table_exists
    from backend.app.models.archive import PrintArchive
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
    from backend.app.models.library_file_note import LibraryFileNote
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.product import ProductPlate, product_files

    duplicated_hashes = (
        (
            await db.execute(
                select(LibraryFile.file_hash)
                .where(LibraryFile.file_hash.isnot(None), LibraryFile.deleted_at.is_(None))
                .group_by(LibraryFile.file_hash)
                .having(func.count(LibraryFile.id) > 1)
            )
        )
        .scalars()
        .all()
    )
    if not duplicated_hashes:
        return 0, 0

    rows = (
        await db.execute(
            select(LibraryFile.id, LibraryFile.file_hash)
            .where(LibraryFile.file_hash.in_(duplicated_hashes), LibraryFile.deleted_at.is_(None))
            .order_by(LibraryFile.id)
        )
    ).all()
    candidate_ids = [row_id for row_id, _ in rows]

    conn = await db.connection()
    sources = [
        (PrintArchive, PrintArchive.library_file_id),
        (PrintQueueItem, PrintQueueItem.library_file_id),
        (AutoQueueItem, AutoQueueItem.library_file_id),
        (LibraryFileNote, LibraryFileNote.library_file_id),
        (LibraryFileMakerworldMeta, LibraryFileMakerworldMeta.library_file_id),
    ]
    if await table_exists(conn, "product_files"):
        sources.append((product_files, product_files.c.library_file_id))
    if await table_exists(conn, "product_plates"):
        sources.append((ProductPlate, ProductPlate.library_file_id))

    references: dict[int, int] = dict.fromkeys(candidate_ids, 0)
    for source, column in sources:
        counted = await db.execute(
            select(column, func.count()).select_from(source).where(column.in_(candidate_ids)).group_by(column)
        )
        for file_id, count in counted.all():
            references[file_id] = references.get(file_id, 0) + count

    # The legacy pair has no model left to select from, so it is asked in raw
    # SQL — and only when the table is actually there.
    for table, column in (("library_file_projects", "file_id"), ("project_print_plan_items", "library_file_id")):
        if not await table_exists(conn, table):
            continue
        counted = await db.execute(
            text(f"SELECT {column}, COUNT(*) FROM {table} WHERE {column} IN :ids GROUP BY {column}").bindparams(
                bindparam("ids", value=candidate_ids, expanding=True)
            )
        )
        for file_id, count in counted.all():
            references[file_id] = references.get(file_id, 0) + count

    grouped: dict[str, list[int]] = {}
    for row_id, file_hash in rows:
        grouped.setdefault(file_hash, []).append(row_id)

    losers: list[int] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        # Most-referenced first; ``-id`` makes the lowest id win a tie.
        ranked = sorted(group, key=lambda file_id: (references.get(file_id, 0), -file_id), reverse=True)
        losers.extend(ranked[1:])

    if not losers:
        return len(grouped), 0

    await db.execute(
        update(LibraryFile).where(LibraryFile.id.in_(losers)).values(deleted_at=datetime.now(timezone.utc))
    )
    return len(grouped), len(losers)
