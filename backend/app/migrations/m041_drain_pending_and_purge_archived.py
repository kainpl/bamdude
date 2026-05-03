"""Drain `pending_uploads` to library + hard-delete legacy `print_archives.status='archived'` rows.

After Audits 1+2 in 0.4.2 (manual archive upload removed; VP queue
ingestion routed through the library) no production code creates fresh
``pending_uploads`` rows, and the upload endpoints that used to write
``status='archived'`` archive rows are gone. The legacy
``pending_uploads.py`` action routes (`POST /archive-all`,
`POST /{id}/archive`) still call ``archive_print(status='archived', ...)``
in 0.4.2 as a backward-compat safety net, but they require pending
rows to act on — after this migration's Part A drains every
``status='pending'`` row, the routes hit their ``Upload already
processed`` early return and produce nothing. Both routes + the table
itself are scheduled for removal in 0.4.3 alongside the rest of the
knock-on cleanup. Existing installations have piles of both legacy
shapes — this migration sunsets them in one shot.

Two independent cleanups bundled because they share the same intent
("purge legacy data left over from the pre-0.4.2 redesign world"):

**Part A — drain `pending_uploads` to library**
For each pending row whose temp file is still on disk, save it to the
library (deduping by hash so re-runs don't multiply rows), link the
new column ``pending_uploads.archived_to_library_id`` to the resulting
``library_files`` row, and flip status from `'pending'` to `'archived'`.
Pending rows whose temp file has gone missing are flipped to
`'discarded'`. Idempotent — re-running a row already at status
`'archived'` / `'discarded'` is a no-op (the WHERE filter excludes them).

The frontend's `PendingUploadsPanel` already returns `null` when the
list is empty, so once this migration runs the panel disappears
entirely; the API + table stay queryable for one release as a safety
net (next release drops both).

**Part B — hard-delete `print_archives` rows with `status='archived'`**
These are the synthetic placeholder rows produced by:

* the now-removed `POST /archives/upload` + `/upload-bulk` (Audit-1)
* the pre-redesign VP `_add_to_print_queue` / `_add_to_auto_queue` paths (Audit-2)
* the pre-redesign `pending_uploads` approval routes (Audit-3 itself)

They have `printer_id=NULL`, `started_at=NULL`, no energy / cost data,
and were already filtered out of stats / list views by the
`status != 'archived'` clause across the codebase. Now that no code
produces them they're safe to hard-delete. We do NOT renumber primary
keys — `print_archives.id` is referenced by `print_queue.archive_id` /
`auto_queue.archive_id` / `active_print_spoolman.archive_id` /
`library_files.archive_id` / `project_bom.archive_id` /
`spool_usage_history.archive_id`, and renumbering would require a
multi-table rewrite for zero practical benefit.

FK handling on hard-delete:

* `print_queue.archive_id` → `ON DELETE SET NULL` (m018) — the queue
  item survives but loses its archive link. (After Audit-2 these
  legacy rows shouldn't exist either, but if any do they keep
  working with NULL.)
* `auto_queue.archive_id` → `ON DELETE CASCADE` (m032 baseline) —
  pre-dispatch routing rows tied to a deleted archive go away.
* `active_print_spoolman.archive_id` → `ON DELETE CASCADE` — active
  spool tracking for a non-existent archive can't be valid.
* `library_files.archive_id` → `ON DELETE SET NULL` — library row
  stays, link goes NULL.
* `project_bom.archive_id` → `ON DELETE SET NULL`.
* `spool_usage_history.archive_id` → no `ON DELETE` clause (defaults
  to NO ACTION). SQLite ignores FK actions without
  ``PRAGMA foreign_keys = ON`` (which BamDude doesn't set globally),
  so we **explicitly pre-NULL** these rows BEFORE the DELETE so both
  SQLite and Postgres end up with the same shape.
* `pending_uploads.archived_id` → `ON DELETE SET NULL` — same
  treatment as library_files.

Disk cleanup mirrors `ArchiveService.delete_archive` ref-counting:
collect each victim's `file_path`'s parent directory, hard-delete the
DB rows in a single statement, then for each unique directory check
if any *surviving* archive row still references a file in that
directory — if not, `shutil.rmtree(dir, ignore_errors=True)`. Cross-
printer file dedup means N legacy rows can share one on-disk file;
without the ref-count check we'd nuke a directory that legitimate
print-history rows still point at.

Idempotent — re-running finds 0 rows to delete.

Schema change:
``pending_uploads.archived_to_library_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL``
— audit trail for which library row an approved pending upload
landed at. Useful if anyone ever wants to trace a pre-0.4.2 pending
into the post-migration library.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from backend.app.core.config import settings as app_settings
from backend.app.migrations.helpers import add_column, table_exists

logger = logging.getLogger(__name__)

version = 41
name = "drain_pending_and_purge_archived"


async def upgrade(conn):
    # Audit-trail column on pending_uploads. SQLite doesn't enforce ON
    # DELETE actions without PRAGMA foreign_keys=ON (which BamDude
    # doesn't set globally), so the FK clause is documentation for
    # Postgres + future-proofing.
    #
    # Guard: a future release will drop the model + the import in
    # ``core/database.py`` once m042 has dropped the table everywhere,
    # at which point fresh installs reach this migration with no
    # ``pending_uploads`` table at all. ``add_column`` would then
    # ALTER a non-existent table and fail. Skip the audit column on
    # those installs — there's nothing to audit since m042 will drop
    # the table later in the same boot.
    if not await table_exists(conn, "pending_uploads"):
        return
    await add_column(
        conn,
        "pending_uploads",
        "archived_to_library_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL",
    )


async def seed(session_factory):  # noqa: PLR0915 - migrations bundle two independent cleanups
    async with session_factory() as db:
        await _drain_pending_uploads(db)
        await _purge_legacy_archived(db)


# --------------------------------------------------------------------------
# Part A — drain pending_uploads
# --------------------------------------------------------------------------


async def _drain_pending_uploads(db) -> None:
    """Save each pending upload's temp file into the library + mark archived.

    File missing on disk → mark discarded. File present → hash, dedup
    against existing ``library_files.file_hash``, save if new. Either
    way the pending row's ``archived_to_library_id`` points at the
    library row that now holds the bytes (or stays NULL on discard).

    No-op when the ``pending_uploads`` table is absent (fresh installs
    on releases that have removed the model + ``database.py`` import).
    """

    if not await table_exists(await db.connection(), "pending_uploads"):
        return

    rows = (
        await db.execute(
            text("SELECT id, filename, file_path, file_size FROM pending_uploads WHERE status = 'pending' ORDER BY id")
        )
    ).all()

    if not rows:
        return

    logger.info("m041: draining %s pending_uploads row(s) to library", len(rows))

    # Local imports — avoid loading at module top so a fresh-install
    # `upgrade()` that runs before models are wired doesn't crash.
    from backend.app.api.routes.library import (
        get_library_files_dir,
        get_library_thumbnails_dir,
        to_relative_path,
    )
    from backend.app.models.library import LibraryFile
    from backend.app.services.archive import ThreeMFParser
    from backend.app.services.library_helpers import compute_file_tags, detect_file_type

    saved = 0
    discarded = 0
    deduped = 0

    for row in rows:
        pending_id, filename, src_path_str, _file_size = row
        src_path = Path(src_path_str) if src_path_str else None

        if not src_path or not src_path.exists():
            await db.execute(
                text("UPDATE pending_uploads SET status = 'discarded', archived_at = :now WHERE id = :id"),
                {"now": datetime.now(timezone.utc), "id": pending_id},
            )
            discarded += 1
            continue

        # Hash first — dedup against any existing library row with the
        # same bytes. Avoids piling up duplicate copies if an admin
        # already manually saved the same file via the library UI.
        sha = hashlib.sha256()
        with open(src_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        file_hash = sha.hexdigest()

        existing_id = (
            await db.execute(
                text("SELECT id FROM library_files WHERE file_hash = :h AND deleted_at IS NULL ORDER BY id LIMIT 1"),
                {"h": file_hash},
            )
        ).scalar()

        if existing_id is not None:
            await db.execute(
                text(
                    "UPDATE pending_uploads SET status = 'archived', "
                    "archived_to_library_id = :lib_id, archived_at = :now "
                    "WHERE id = :id"
                ),
                {"lib_id": existing_id, "now": datetime.now(timezone.utc), "id": pending_id},
            )
            try:
                src_path.unlink()
            except OSError:
                pass
            deduped += 1
            continue

        # Persist a fresh library row.
        ext = src_path.suffix.lower()
        library_files_dir = get_library_files_dir()
        unique_filename = f"{uuid.uuid4().hex}{ext}"
        dest_path = library_files_dir / unique_filename

        try:
            shutil.copy2(str(src_path), str(dest_path))
        except OSError as e:
            logger.warning("m041: failed to copy pending %s → %s: %s", pending_id, dest_path, e)
            await db.execute(
                text("UPDATE pending_uploads SET status = 'discarded', archived_at = :now WHERE id = :id"),
                {"now": datetime.now(timezone.utc), "id": pending_id},
            )
            discarded += 1
            continue

        # 3MF metadata + thumbnail extraction (best-effort).
        metadata = None
        thumbnail_rel: str | None = None
        if ext == ".3mf":
            try:
                parser = ThreeMFParser(str(dest_path))
                raw = parser.parse()

                thumb_data = raw.get("_thumbnail_data")
                thumb_ext = raw.get("_thumbnail_ext", ".png")
                if thumb_data:
                    thumbs_dir = get_library_thumbnails_dir()
                    thumb_filename = f"{uuid.uuid4().hex}{thumb_ext}"
                    thumb_path = thumbs_dir / thumb_filename
                    with open(thumb_path, "wb") as fh:
                        fh.write(thumb_data)
                    thumbnail_rel = to_relative_path(thumb_path)

                def _clean(obj):
                    if isinstance(obj, dict):
                        return {
                            k: _clean(v)
                            for k, v in obj.items()
                            if not isinstance(v, bytes) and k not in ("_thumbnail_data", "_thumbnail_ext")
                        }
                    if isinstance(obj, list):
                        return [_clean(i) for i in obj if not isinstance(i, bytes)]
                    if isinstance(obj, bytes):
                        return None
                    return obj

                metadata = _clean(raw)
            except Exception as e:  # noqa: BLE001 — parser is best-effort
                logger.debug("m041: 3MF parse failed for pending %s: %s", pending_id, e)

        file_type = detect_file_type(filename)
        tags = compute_file_tags(
            filename=filename,
            file_type=file_type,
            file_metadata=metadata,
            source_type=None,
            swap_compatible=False,
        )

        # Use the ORM so model-side defaults (is_external,
        # swap_compatible, print_count, file_tags fallback, …) apply
        # without us having to mirror the column list by hand. Raw text()
        # INSERTs would have to enumerate every NOT NULL column or hit
        # an IntegrityError on the booleans that lack server_default.
        new_lib = LibraryFile(
            folder_id=None,
            filename=filename,
            file_path=to_relative_path(dest_path),
            file_type=file_type,
            file_tags=tags,
            file_size=dest_path.stat().st_size,
            file_hash=file_hash,
            thumbnail_path=thumbnail_rel,
            file_metadata=metadata,
        )
        db.add(new_lib)
        await db.flush()  # populate new_lib.id without committing the txn yet
        new_lib_id = new_lib.id

        await db.execute(
            text(
                "UPDATE pending_uploads SET status = 'archived', "
                "archived_to_library_id = :lib_id, archived_at = :now "
                "WHERE id = :id"
            ),
            {"lib_id": new_lib_id, "now": datetime.now(timezone.utc), "id": pending_id},
        )

        try:
            src_path.unlink()
        except OSError:
            pass

        saved += 1
        if (saved + deduped + discarded) % 100 == 0:
            await db.commit()
            logger.info(
                "m041: pending drain progress — saved=%s deduped=%s discarded=%s",
                saved,
                deduped,
                discarded,
            )

    await db.commit()
    logger.info(
        "m041: pending_uploads drain done — saved=%s deduped=%s discarded=%s",
        saved,
        deduped,
        discarded,
    )


# --------------------------------------------------------------------------
# Part B — purge legacy `status='archived'` print_archives rows
# --------------------------------------------------------------------------


async def _purge_legacy_archived(db) -> None:
    """Hard-delete legacy `status='archived'` archives + ref-counted disk cleanup.

    Pre-NULLs ``spool_usage_history.archive_id`` for victims (no ON
    DELETE clause on that FK), then DELETEs in one statement, then
    walks the unique directory set and rmtree's any directory that
    no surviving archive row references. Cross-printer file dedup
    means several rows can share an on-disk file; the survivor check
    is what prevents accidentally nuking a path still in use.
    """

    victims = (
        await db.execute(
            text("SELECT id, file_path, thumbnail_path FROM print_archives WHERE status = 'archived' ORDER BY id")
        )
    ).all()

    if not victims:
        return

    logger.info("m041: hard-deleting %s legacy print_archives rows (status='archived')", len(victims))

    victim_ids = [row[0] for row in victims]
    candidate_dirs: set[Path] = set()

    base_dir = Path(app_settings.base_dir)
    for _id, file_path, thumb_path in victims:
        for rel in (file_path, thumb_path):
            if not rel:
                continue
            try:
                abs_path = base_dir / rel
                candidate_dirs.add(abs_path.parent)
            except (TypeError, ValueError):
                continue

    # Pre-NULL spool_usage_history.archive_id (no FK ON DELETE clause —
    # leaving stale FKs would orphan surviving spool_usage_history rows
    # against deleted archives, and the read paths assume archive_id
    # either resolves or is NULL).
    for chunk in _chunked(victim_ids, 500):
        placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
        params = {f"id{i}": pid for i, pid in enumerate(chunk)}
        await db.execute(
            text(f"UPDATE spool_usage_history SET archive_id = NULL WHERE archive_id IN ({placeholders})"),
            params,
        )

    # Hard-delete in chunks (SQLite IN-list cap ~999, Postgres handles
    # millions but chunking caps memory).
    deleted_total = 0
    for chunk in _chunked(victim_ids, 500):
        placeholders = ",".join(f":id{i}" for i in range(len(chunk)))
        params = {f"id{i}": pid for i, pid in enumerate(chunk)}
        result = await db.execute(
            text(f"DELETE FROM print_archives WHERE id IN ({placeholders})"),
            params,
        )
        deleted_total += getattr(result, "rowcount", 0) or len(chunk)
        await db.commit()

    logger.info("m041: hard-deleted %s legacy archive rows", deleted_total)

    # Disk cleanup — only rmtree directories that no surviving archive
    # row references.
    rmtree_count = 0
    for cand_dir in candidate_dirs:
        if not cand_dir.exists():
            continue

        # Safety: must be inside the archive_dir.
        try:
            cand_dir.resolve().relative_to(Path(app_settings.archive_dir).resolve())
        except (ValueError, OSError):
            logger.warning("m041: refusing to rmtree outside archive_dir: %s", cand_dir)
            continue

        try:
            cand_rel = str(cand_dir.relative_to(base_dir))
        except ValueError:
            continue

        # Anything under this directory that a surviving archive row points at?
        survivor = (
            await db.execute(
                text(
                    "SELECT 1 FROM print_archives WHERE file_path LIKE :prefix OR thumbnail_path LIKE :prefix LIMIT 1"
                ),
                {"prefix": f"{cand_rel}%"},
            )
        ).scalar()

        if survivor:
            continue

        try:
            shutil.rmtree(cand_dir, ignore_errors=True)
            rmtree_count += 1
        except OSError as e:
            logger.warning("m041: rmtree failed for %s: %s", cand_dir, e)

        if rmtree_count and rmtree_count % 100 == 0:
            logger.info("m041: rmtree progress — %s directories cleaned", rmtree_count)

    logger.info("m041: disk cleanup done — %s directories removed", rmtree_count)


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
