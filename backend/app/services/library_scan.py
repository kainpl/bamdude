"""Incremental external-folder scan using the shared library file worker.

The worker walks, stats, hashes and parses outside main; this module holds only
short DB transactions for folder/file publication. A failed or incomplete walk
never enters the destructive deletion pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_scan import LibraryScanJob
from backend.app.services.domain_authority import guard
from backend.app.services.product_sync import inherit_folder_products, purge_file_product_links, resync_file_products

logger = logging.getLogger(__name__)

#: How many files are written per transaction.
#:
#: ⚠️ Not about the cost of committing — in WAL with ``synchronous = NORMAL`` a
#: commit does no fsync. It is about how long the write lock is held: fifty rows
#: is a window of milliseconds, while cutting the number of commits fiftyfold
#: against one-per-file.
BATCH_SIZE = 50

#: The floor between two progress broadcasts.
#:
#: ⚠️ Per-file progress on a five-thousand-file share is five thousand messages
#: to every open tab. The per-file ``library_file_added`` stays unthrottled — it
#: carries a row somebody wants to see appear — but progress is a number nobody
#: reads five thousand times.
PROGRESS_INTERVAL_SECONDS = 0.5


def _now() -> datetime:
    """⚠️ UTC, naive — every timestamp in this database is."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class _Known:
    """What is already stored about a file, kept as plain data.

    ⚠️ Deliberately not the ORM row. Rows belong to a session, and this survives
    across the many short ones a scan opens — holding a detached instance and
    touching it later is how this kind of loop grows mysterious lazy-load
    errors.
    """

    id: int
    file_hash: str | None
    file_size: int | None
    fs_modified_at: datetime | None
    #: The stored ``has_sliced_gcode`` — ``None`` when the row never got one
    #: (every row this scan wrote before it asked, upstream #2993).
    sliced: bool | None = None
    extraction_version: int | None = None
    extraction_hash: str | None = None


@dataclass
class _Prepared:
    """One file, examined off the event loop, ready to be written."""

    path: Path
    file_path_str: str
    filename: str
    folder_rel: str
    size: int
    fs_modified_at: datetime
    #: ``create`` for a file with no row yet, ``refresh`` for one that has.
    intent: str
    known_id: int | None = None
    new_hash: str | None = None
    mtime_changed: bool = False
    file_type: str | None = None
    content_hash: str | None = None
    thumbnail_path: str | None = None
    file_metadata: dict | None = field(default=None)
    #: A refresh's new answer to "does this 3MF hold G-code", set only when it
    #: differs from what the row stores.
    sliced: bool | None = None
    metadata_complete: bool = False
    expected_hash: str | None = None
    #: The swap declaration a NEW content identity publishes with its hash and
    #: metadata -- the share's own entry name, classified. ``None`` keeps the
    #: row's: the same bytes, or a repair of a row whose hash was missing.
    swap_declaration: bool | None = None


async def prepare_via_service(
    dirpath: str,
    filename: str,
    root: Path,
    known: _Known | None,
    folder_rel: str,
    size: int,
    mtime_ns: int,
) -> _Prepared | None:
    """The scan's production path through the shared local file worker."""
    from backend.app.api.routes.library import _mtime_to_utc, get_library_thumbnails_dir, to_relative_path
    from backend.app.services import swap_compatibility
    from backend.app.services.library_file_preparation import EXTRACTION_VERSION, SCANNABLE_EXTENSIONS
    from backend.app.services.library_file_runtime import get_library_file_runtime

    path = Path(dirpath) / filename  # SEC-PATH-OK: a name walk_next(root) listed; prepare_file keeps root
    if path.suffix.lower() not in SCANNABLE_EXTENSIONS:
        return None
    runtime = get_library_file_runtime()
    mtime = _mtime_to_utc(mtime_ns / 1e9)
    # ⚠️ A complete extraction needs a VALID digest (R2-3): NULL == NULL, or two
    # equal invalid strings, is not one. Such a row is prepared again even with
    # unchanged size and mtime, and the refresh below repairs it.
    complete = (
        known is not None
        and swap_compatibility.valid_hash(known.file_hash)
        and known.extraction_version == EXTRACTION_VERSION
        and known.extraction_hash == known.file_hash
        and (not filename.lower().endswith(".3mf") or known.sliced is not None)
    )
    if known and complete and known.file_size == size and known.fs_modified_at == mtime:
        return None
    if known and complete:
        revision = await runtime.hash(path, root=root)
        if revision["digest"] == known.file_hash:
            return _Prepared(
                path=path,
                file_path_str=str(path),
                filename=filename,
                folder_rel=folder_rel,
                size=revision["size"],
                fs_modified_at=_mtime_to_utc(revision["mtime_ns"] / 1e9),
                intent="refresh",
                known_id=known.id,
                new_hash=revision["digest"],
                mtime_changed=known.fs_modified_at != mtime,
                expected_hash=known.file_hash,
            )
    result = await runtime.prepare(path, root=root, filename=filename)
    # A new content identity on this path declares again, by the share's own
    # entry name (never a display rename). The same bytes, or the repair of a
    # row whose hash was missing or invalid, keep what the row declares.
    new_identity = known is None or (
        swap_compatibility.valid_hash(known.file_hash) and result.digest != known.file_hash
    )
    thumbnail_path = None
    if result.thumbnail:
        extension = result.thumbnail_ext if result.thumbnail_ext in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
        thumb = get_library_thumbnails_dir() / f"{uuid.uuid4().hex}{extension}"
        await asyncio.to_thread(thumb.write_bytes, result.thumbnail)
        thumbnail_path = to_relative_path(thumb)
    return _Prepared(
        path=path,
        file_path_str=str(path),
        filename=filename,
        folder_rel=folder_rel,
        size=result.size,
        fs_modified_at=_mtime_to_utc(result.mtime_ns / 1e9),
        intent="refresh" if known else "create",
        known_id=known.id if known else None,
        new_hash=result.digest if known else None,
        content_hash=result.digest,
        mtime_changed=known.fs_modified_at != mtime if known else False,
        file_type=result.file_type,
        file_metadata=result.metadata,
        thumbnail_path=thumbnail_path,
        metadata_complete=True,
        expected_hash=known.file_hash if known else None,
        swap_declaration=swap_compatibility.classify_external_name(filename) if new_identity else None,
    )


# ── Writing a batch, with the session held for as little as possible ─────────


async def write_batch(
    batch: list[_Prepared],
    folder_ids: dict[str, int],
    counters: dict[str, int],
    *,
    authority=None,
) -> tuple[list[tuple[int, str]], int]:
    """Persist one batch and return created rows and skipped duplicates.

    ⚠️ The session is opened here and closed on the way out. Everything this
    needs was worked out before it was called, so the write lock is held for the
    length of a few INSERTs rather than the length of a scan.
    """
    from backend.app.api.routes.library import _without_print_name, to_absolute_path
    from backend.app.core.database import async_session
    from backend.app.services.library_file_runtime import get_library_file_runtime
    from backend.app.services.library_helpers import (
        SLICED_GCODE_META_KEY,
        skip_objects_supported_from_metadata,
        sync_system_tags,
    )
    from backend.app.services.library_ingest import _rows_with_hash

    # Check duplicate candidates before opening the write session. A mounted
    # share can stall on a presence check, and no DB transaction should stay
    # open while that happens. The worker performs the filesystem I/O.
    candidate_hashes = {item.content_hash for item in batch if item.intent == "create" and item.content_hash}
    candidates: dict[str, list[str]] = {}
    if candidate_hashes:
        async with async_session() as read_db:
            for digest in candidate_hashes:
                rows = await _rows_with_hash(read_db, digest)
                paths = []
                for row in rows:
                    with contextlib.suppress(Exception):
                        absolute = to_absolute_path(row.file_path)
                        if absolute:
                            paths.append(str(absolute))
                candidates[digest] = paths
    duplicate_present: set[str] = set()
    runtime = get_library_file_runtime()
    for digest, paths in candidates.items():
        for path in paths:
            if (await runtime.present(Path(path).parent, [path]))[0]:
                duplicate_present.add(digest)
                break

    created: list[tuple[int, str]] = []
    skipped_duplicates = 0
    #: Ids of the rows this batch actually rewrote — the ones whose bytes moved
    #: on disk, so a product that owns plates off them has to be reconciled.
    refreshed: list[int] = []
    retired_thumbnails: list[str] = []

    from backend.app.core.database import take_write_lock
    from backend.app.services import swap_compatibility

    # Refreshes first, in ascending id: each row is read under the write protocol a declaration also takes
    # (swap-source-declaration spec r3 §5) -- SQLite's writer before the first authoritative read, PostgreSQL's row
    # lock -- so a declaration and this batch never interleave between a read and its write.
    batch = sorted(batch, key=lambda item: (item.intent != "refresh", item.known_id or 0))
    async with async_session() as db:
        await guard(db, authority, permissions=("library:upload",))
        first_refresh = next((item.known_id for item in batch if item.intent == "refresh"), None)
        if first_refresh is not None:
            await take_write_lock(db, LibraryFile.__table__, first_refresh)
        for item in batch:
            if item.intent == "refresh":
                row = (
                    await db.execute(
                        select(LibraryFile)
                        .where(LibraryFile.id == item.known_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.file_path != item.file_path_str
                    or (item.metadata_complete and row.file_hash != item.expected_hash)
                ):
                    continue
                values: dict = {}
                if item.mtime_changed:
                    # ⚠️ Assigned only when it moved. A no-op write still fires
                    # ``onupdate`` and stamps ``updated_at`` on every row of
                    # every scan, re-creating the very tie the column exists to
                    # break.
                    values["fs_modified_at"] = item.fs_modified_at
                if item.new_hash:
                    values["file_hash"] = item.new_hash
                    values["file_size"] = item.size
                if item.metadata_complete:
                    values["file_metadata"] = _without_print_name(item.file_metadata)
                    values["skip_objects_supported"] = skip_objects_supported_from_metadata(item.file_metadata)
                    if row.thumbnail_path and row.thumbnail_path != item.thumbnail_path:
                        retired_thumbnails.append(row.thumbnail_path)
                    values["thumbnail_path"] = item.thumbnail_path
                    # Published with the hash and metadata it belongs to, under the
                    # same check that the row still holds what was read: new bytes get
                    # a new classifier carrier, a hashless legacy row §8's
                    # classification, the same bytes keep what they declare.
                    values.update(
                        swap_compatibility.scan_declaration(
                            row,
                            new_hash=item.new_hash,
                            new_identity=item.swap_declaration is not None,
                            entry_name=item.filename,
                        )
                    )
                if values:
                    await db.execute(update(LibraryFile).where(LibraryFile.id == item.known_id).values(**values))
                if item.metadata_complete:
                    await db.refresh(row)
                    await sync_system_tags(db, row)
                elif item.sliced is not None:
                    # The answer moved, so the tags that gate Print move with it.
                    row = await db.get(LibraryFile, item.known_id)
                    if row is not None:
                        row.file_metadata = {**(row.file_metadata or {}), SLICED_GCODE_META_KEY: item.sliced}
                        await sync_system_tags(db, row)
                if values or item.sliced is not None:
                    counters["files_updated"] += 1
                    if item.known_id is not None:
                        refreshed.append(item.known_id)
                continue

            if item.content_hash in duplicate_present:
                # The library already holds these bytes. Counted rather than
                # silent: a scan is also how people browse a mount, and a
                # skipped file reads as a scan that missed something.
                skipped_duplicates += 1
                continue

            db_file = LibraryFile(
                folder_id=folder_ids[item.folder_rel],
                is_external=True,
                filename=item.filename,
                file_path=item.file_path_str,
                file_type=item.file_type,
                skip_objects_supported=skip_objects_supported_from_metadata(item.file_metadata),
                file_size=item.size,
                file_hash=item.content_hash,
                thumbnail_path=item.thumbnail_path,
                file_metadata=_without_print_name(item.file_metadata),
                fs_modified_at=item.fs_modified_at,
            )
            swap_compatibility.declare_ingest(db_file, item.swap_declaration is True, origin="classifier")
            db.add(db_file)
            await db.flush()
            await sync_system_tags(db, db_file)
            await inherit_folder_products(db, db_file, await db.get(LibraryFolder, db_file.folder_id))
            counters["files_added"] += 1
            created.append((db_file.id, db_file.filename))

        # A file somebody re-sliced under a linked product must not leave the
        # product owning plate 2 of a slice that now has one plate. The resync
        # is a handful of small statements per file and rides inside this
        # batch's own short transaction — the m148 rule holds: the walk is
        # already over by the time this session was opened.
        #
        # ⚠️ Only rows this batch rewrote, and only those already in
        # ``product_files`` — ``resync_file_products`` returns on the first
        # SELECT for the overwhelming majority that belong to no product.
        #
        # It reconciles against the row's freshly published plate snapshot.
        #
        # ⚠️ Best-effort, per file, like ``seed_archive_parts``: a scan must not
        # die because one product's composition could not be reconciled. The
        # batch's own writes are already made and still commit below.
        for library_file_id in refreshed:
            try:
                await resync_file_products(db, library_file_id)
            except Exception:
                logger.warning("product resync failed for library file %s", library_file_id, exc_info=True)

        await db.commit()

    if retired_thumbnails:
        from backend.app.api.routes.library import to_absolute_path

        for old in retired_thumbnails:
            with contextlib.suppress(OSError):
                path = to_absolute_path(old)
                if path:
                    await asyncio.to_thread(path.unlink, missing_ok=True)

    return created, skipped_duplicates


async def ensure_folders(
    db: AsyncSession,
    root: Path,
    root_folder: LibraryFolder,
    directories: list[str],
    folder_ids: dict[str, int],
    counters: dict[str, int],
    *,
    authority=None,
) -> None:
    """Create the subfolder rows the walk turned up.

    Done before the files and in its own short transactions: a file row needs a
    folder id, and discovering that mid-batch is what tied the old scan's
    folder writes to its file writes.
    """
    await guard(db, authority, permissions=("library:upload",))
    for dirpath in directories:
        rel = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        if rel == ".":
            rel = ""
        if rel in folder_ids:
            continue

        parent_id = folder_ids[""]
        current = ""
        for part in rel.split("/"):
            current = f"{current}/{part}" if current else part
            if current in folder_ids:
                parent_id = folder_ids[current]
                continue
            new_folder = LibraryFolder(
                name=part,
                parent_id=parent_id,
                is_external=True,
                # SEC-PATH-OK: `current` is relative_to(root) of a walked on-disk
                # directory, never request input.
                external_path=str(root / current),
                external_show_hidden=root_folder.external_show_hidden,
            )
            db.add(new_folder)
            await db.flush()
            folder_ids[current] = new_folder.id
            counters["folders_added"] += 1
            parent_id = new_folder.id


# ── The deletion pass, and the guard it needs ───────────────────────────────


#: A walk that found nothing, against a folder that has this many rows or more,
#: is treated as an unreachable mount rather than an emptied folder.
#:
#: ⚠️ One record is enough to be worth protecting, so the threshold is 1: the
#: question is not "how many" but "did the walk see anything at all".
EMPTY_WALK_GUARD = 1


async def remove_vanished(
    found_paths: set[str],
    known: dict[str, _Known],
    folder_ids: dict[str, int],
    counters: dict[str, int],
    *,
    root: Path | None = None,
    authority=None,
) -> bool:
    """Drop rows whose file is gone. Returns whether deletion was refused.

    ⚠️ **A share that blinked looks exactly like a folder somebody emptied.**
    ``os.path.exists`` on a disconnected Synology mount says no to everything,
    and an honest sync then deletes a library nobody touched. So a walk that
    found no files at all against a folder that has rows is read as an
    unreachable mount: nothing is removed, and the job says so.

    ⚠️ The per-path ``os.path.exists`` check stays (it is #2520 — a ``.md``
    README was being purged on every scan for not being a scannable extension).
    It is also what makes an interrupted scan safe: rows go on disk absence, not
    on "this walk did not reach it".
    """
    from backend.app.api.routes.library import to_absolute_path
    from backend.app.core.database import async_session

    if not found_paths and len(known) >= EMPTY_WALK_GUARD:
        logger.warning(
            "scan found no files where %d are on record — treating the mount as unreachable and deleting nothing",
            len(known),
        )
        return True

    absent_candidates = [(entry.id, path) for path, entry in known.items() if path not in found_paths]
    if root is not None:
        from backend.app.services.library_file_runtime import get_library_file_runtime

        runtime = get_library_file_runtime()
        presence = []
        for start in range(0, len(absent_candidates), 32):
            presence.extend(await runtime.present(root, [path for _, path in absent_candidates[start : start + 32]]))
    else:
        presence = [await asyncio.to_thread(os.path.exists, path) for _, path in absent_candidates]
    doomed = [candidate for candidate, present in zip(absent_candidates, presence, strict=True) if not present]
    if not doomed:
        return False

    for start in range(0, len(doomed), BATCH_SIZE):
        chunk = doomed[start : start + BATCH_SIZE]
        async with async_session() as db:
            await guard(db, authority, permissions=("library:upload",))
            rows = (
                (await db.execute(select(LibraryFile).where(LibraryFile.id.in_([i for i, _ in chunk])))).scalars().all()
            )
            # A vanished external source must not take a job that has already
            # captured its own bytes with it.  The hard-delete helper also
            # nulls both tiers explicitly, which SQLite otherwise leaves as
            # dangling foreign-key values.
            from backend.app.services import queue_source_release

            await queue_source_release.source_purged(
                db,
                library_file_ids=[row.id for row in rows],
                reason=queue_source_release.REASON_FILE_VANISHED,
            )
            # Before the deletes, once for the chunk: the ORM clears the
            # ``product_files`` pivot but not ``product_plates``, whose cascade
            # only fires on PostgreSQL.
            await purge_file_product_links(db, [row.id for row in rows])
            for row in rows:
                if row.thumbnail_path:
                    with contextlib.suppress(OSError):
                        abs_thumb = to_absolute_path(row.thumbnail_path)
                        if abs_thumb and abs_thumb.exists():
                            abs_thumb.unlink()
                await db.delete(row)
                counters["files_removed"] += 1
            await db.commit()

    # Subfolder rows whose directory is gone, deepest first so a parent is only
    # considered once its children have left.
    if root is not None:
        folder_presence = {}
        paths = [str(root / rel) for rel in folder_ids if rel]
        for start in range(0, len(paths), 32):
            found = await runtime.present(root, paths[start : start + 32])
            folder_presence.update(zip(paths[start : start + 32], found, strict=True))
    else:
        folder_presence = {}
    async with async_session() as db:
        await guard(db, authority, permissions=("library:upload",))
        subs = (
            (
                await db.execute(
                    select(LibraryFolder).where(
                        LibraryFolder.id.in_([fid for rel, fid in folder_ids.items() if rel != ""])
                    )
                )
            )
            .scalars()
            .all()
        )
        subs = sorted(subs, key=lambda f: (f.external_path or "").count("/"), reverse=True)
        for sub in subs:
            if not sub.external_path or (
                folder_presence.get(sub.external_path, True)
                if root is not None
                else await asyncio.to_thread(os.path.exists, sub.external_path)
            ):
                continue
            files_left = (
                await db.execute(select(LibraryFile.id).where(LibraryFile.folder_id == sub.id).limit(1))
            ).first()
            if files_left:
                continue
            children = (
                await db.execute(select(LibraryFolder.id).where(LibraryFolder.parent_id == sub.id).limit(1))
            ).first()
            if children:
                continue
            await db.delete(sub)
            counters["folders_removed"] += 1
        await db.commit()

    return False


# ── The worker ───────────────────────────────────────────────────────────────


#: Scans in flight, so shutdown can cancel them and a second start can be
#: refused.
#:
#: ⚠️ In memory ON TOP of the job row, not instead of it. The row is what
#: survives to be swept after a restart; this is what can actually be cancelled
#: while the process is alive. Neither replaces the other.
_running: dict[int, asyncio.Task] = {}


async def _set_job(job_id: int, *, authority=None, **values) -> None:
    """Write a few columns of the job and let go of the session immediately."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        if authority is not None:
            await guard(db, authority, permissions=("library:upload",))
        await db.execute(update(LibraryScanJob).where(LibraryScanJob.id == job_id).values(**values))
        await db.commit()


async def _announce_finish(payload: dict) -> None:
    """Tell every open tab that a scan ended.

    Best effort: a socket problem must never turn a scan whose rows are already
    committed into a failed one.
    """
    from backend.app.core.websocket import ws_manager

    with contextlib.suppress(Exception):
        await ws_manager.send_library_scan_finished(payload)


async def _fail_job(job_id: int, folder_id: int | None, error: str) -> None:
    """Record a failure, and say so on the socket.

    ⚠️ Both halves, always. A row that reads ``failed`` while the tabs were
    never told leaves a progress strip spinning forever — and the commonest
    failure here is an unreachable mount, which is exactly when somebody is
    sitting and watching that strip.
    """
    await _set_job(job_id, status="failed", error=error[:2000], finished_at=_now())
    await _announce_finish({"job_id": job_id, "folder_id": folder_id, "status": "failed", "error": error[:500]})


async def run_scan(job_id: int, authority=None) -> None:
    """Walk the folder, write what changed, and keep the database free meanwhile."""
    from backend.app.core.database import async_session
    from backend.app.core.websocket import ws_manager

    counters = {
        "files_seen": 0,
        "files_added": 0,
        "files_updated": 0,
        "files_removed": 0,
        "folders_added": 0,
        "folders_removed": 0,
    }
    skipped_duplicates = 0
    # Resolved a few lines down; declared here so every failure path can name
    # the folder whose strip has to stop spinning.
    folder_id: int | None = None
    walk_token: str | None = None
    runtime = None

    try:
        async with async_session() as db:
            job = await db.get(LibraryScanJob, job_id)
            if job is None:
                return
            folder = await db.get(LibraryFolder, job.folder_id)
            if folder is None or not folder.is_external or not folder.external_path:
                await _fail_job(job_id, folder.id if folder else None, "folder is not an external mount")
                return
            root = Path(folder.external_path)
            show_hidden = bool(folder.external_show_hidden)
            folder_id = folder.id

        await _set_job(job_id, authority=authority, status="running", started_at=_now())

        from backend.app.services.library_file_runtime import get_library_file_runtime

        runtime = get_library_file_runtime()
        try:
            walk_token = await runtime.walk_start(root, show_hidden)
        except Exception:
            await _fail_job(job_id, folder_id, f"external path is not accessible: {root}")
            return
        total = 0

        # Read once into plain data, so nothing is carried between the short
        # sessions that follow.
        async with async_session() as db:
            folder_ids: dict[str, int] = {"": folder_id}
            queue = [folder_id]
            while queue:
                parent = queue.pop()
                rows = (
                    await db.execute(
                        select(LibraryFolder.id, LibraryFolder.external_path).where(LibraryFolder.parent_id == parent)
                    )
                ).all()
                for child_id, child_path in rows:
                    queue.append(child_id)
                    if child_path:
                        with contextlib.suppress(ValueError):
                            rel = str(Path(child_path).relative_to(root)).replace("\\", "/")
                            folder_ids[rel] = child_id

            known: dict[str, _Known] = {}
            for row_id, path, digest, size, mtime, sliced, version, extraction_hash in (
                await db.execute(
                    select(
                        LibraryFile.id,
                        LibraryFile.file_path,
                        LibraryFile.file_hash,
                        LibraryFile.file_size,
                        LibraryFile.fs_modified_at,
                        # The one key, not the whole metadata blob — a plate
                        # list per row, for every file on the mount, is not
                        # something a scan needs to hold.
                        LibraryFile.file_metadata["has_sliced_gcode"].as_boolean(),
                        LibraryFile.file_metadata["_library_extraction"]["version"].as_integer(),
                        LibraryFile.file_metadata["_library_extraction"]["hash"].as_string(),
                    ).where(LibraryFile.folder_id.in_(list(folder_ids.values())))
                )
            ).all():
                known[path] = _Known(
                    id=row_id,
                    file_hash=digest,
                    file_size=size,
                    fs_modified_at=mtime,
                    sliced=sliced,
                    extraction_version=version,
                    extraction_hash=extraction_hash,
                )

        found_paths: set[str] = set()
        batch: list[_Prepared] = []
        last_progress = 0.0
        warnings = 0

        async def flush(force: bool = False) -> None:
            nonlocal batch, last_progress, skipped_duplicates
            if batch and (force or len(batch) >= BATCH_SIZE):
                created, skipped_in_batch = await write_batch(batch, folder_ids, counters, authority=authority)
                skipped_duplicates += skipped_in_batch
                batch = []
                for file_id, filename in created:
                    # Best effort: a socket problem must never fail a scan whose
                    # rows are already committed.
                    with contextlib.suppress(Exception):
                        await ws_manager.send_library_file_added({"id": file_id, "filename": filename})

            now = time.monotonic()
            if force or now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                last_progress = now
                with contextlib.suppress(Exception):
                    await ws_manager.send_library_scan_progress(
                        {"job_id": job_id, "folder_id": folder_id, "total": total, **counters}
                    )

        while True:
            page = await runtime.walk_next(root, walk_token)
            entries = page["entries"]
            directories = [entry["directory"] for entry in entries if entry["is_dir"]]
            if directories:
                async with async_session() as db:
                    await ensure_folders(db, root, folder, directories, folder_ids, counters, authority=authority)
                    await db.commit()
            for entry in entries:
                if entry["is_dir"]:
                    continue
                dirpath = entry["directory"]
                filename = entry["name"]
                rel = str(Path(dirpath).relative_to(root)).replace("\\", "/")
                if rel == ".":
                    rel = ""
                counters["files_seen"] += 1
                total += 1
                candidate_path = str(Path(dirpath) / filename)  # SEC-PATH-OK: dirpath/name come from walk_next(root)
                found_paths.add(candidate_path)
                try:
                    prepared = await prepare_via_service(
                        dirpath,
                        filename,
                        root,
                        known.get(candidate_path),
                        rel,
                        entry["size"],
                        entry["mtime_ns"],
                    )
                except Exception:
                    warnings += 1
                    logger.warning("library scan skipped unreadable file %s", candidate_path, exc_info=True)
                    continue
                if prepared is not None:
                    batch.append(prepared)
                    await flush()
            if page["done"]:
                break

        await flush(force=True)

        skipped = await remove_vanished(found_paths, known, folder_ids, counters, root=root, authority=authority)
        if skipped_duplicates:
            logger.info("scan skipped %d file(s) the library already holds", skipped_duplicates)
        if warnings:
            logger.warning("scan finished with %d unreadable file(s); last good metadata kept", warnings)

        await _set_job(
            job_id,
            authority=authority,
            status="finished",
            finished_at=_now(),
            files_total=total,
            skipped_deletions=skipped,
            **counters,
        )
        await _announce_finish(
            {
                "job_id": job_id,
                "folder_id": folder_id,
                "status": "finished",
                "skipped_deletions": skipped,
                "warnings": warnings,
                "total": total,
                **counters,
            }
        )

    except asyncio.CancelledError:
        # A shutdown mid-scan. The row is deliberately NOT written here — this
        # task may not get another await, and the startup sweeper is what exists
        # to answer for jobs whose process went away.
        raise
    except Exception as error:
        logger.exception("external folder scan failed")
        message = str(error)
        if isinstance(error, FileNotFoundError) or "FileNotFoundError" in message:
            message = f"External folder is not accessible: {root}"
        await _fail_job(job_id, folder_id, message)
    finally:
        if walk_token and runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.walk_end(root, walk_token)
        _running.pop(job_id, None)


async def start_scan(folder_id: int, user_id: int | None, *, authority=None) -> int:
    """Create the job row and set the worker going. Returns the job id."""
    from backend.app.core.database import async_session

    if not _admitting:
        from fastapi import HTTPException

        raise HTTPException(503, "Folder scans are unavailable during restore")
    async with async_session() as db:
        _, principal = await guard(db, authority, permissions=("library:upload",))
        if principal is not None:
            user_id = principal.user.id
        job = LibraryScanJob(folder_id=folder_id, status="queued", created_by=user_id)
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    _running[job_id] = asyncio.create_task(run_scan(job_id, authority))
    return job_id


async def active_job_id(db: AsyncSession, folder_id: int) -> int | None:
    """The scan already running for this folder, if there is one.

    ⚠️ Two walks writing the same rows is not twice as fast, it is a race — the
    second would keep finding half-written state from the first.
    """
    target = await db.get(LibraryFolder, folder_id)
    if target is None or not target.external_path:
        return None
    candidates = (
        await db.execute(
            select(LibraryScanJob.id, LibraryFolder.external_path)
            .join(LibraryFolder, LibraryScanJob.folder_id == LibraryFolder.id)
            .where(LibraryScanJob.status.in_(("queued", "running")))
        )
    ).all()
    target_path = os.path.normcase(os.path.abspath(target.external_path))
    for job_id, path in candidates:
        if path:
            candidate_path = os.path.normcase(os.path.abspath(path))
            try:
                if os.path.commonpath([target_path, candidate_path]) in {target_path, candidate_path}:
                    return job_id
            except ValueError:
                continue
    return None


async def sweep_interrupted_jobs() -> int:
    """Fail any job a restart left behind. Returns how many.

    ⚠️ Two things go wrong without this, and the second is worse. `running`
    reads on screen as progress that will never arrive — and the duplicate guard
    above sees an active job, so that folder can never be scanned again.
    """
    from backend.app.core.database import async_session

    async with async_session() as db:
        result = await db.execute(
            update(LibraryScanJob)
            .where(LibraryScanJob.status.in_(("queued", "running")))
            .values(status="failed", error="interrupted by a restart", finished_at=_now())
        )
        await db.commit()
        return result.rowcount or 0


def cancel_running_scans() -> None:
    """Stop every scan in flight. Called from the lifespan shutdown."""
    for task in list(_running.values()):
        task.cancel()
    _running.clear()


_admitting = True


def close_admission() -> None:
    global _admitting
    _admitting = False


async def quiesce_for_restore(seconds: float = 30.0) -> bool:
    close_admission()
    tasks = set(_running.values())
    for task in tasks:
        task.cancel()
    if not tasks:
        return True
    _, pending = await asyncio.wait(tasks, timeout=seconds)
    return not pending
