"""Scanning an external folder without holding the database while it happens.

The scan used to be the request. It opened a write transaction on the first
subfolder it discovered and committed after the last file — with the entire walk
of the share in between. On a NAS that is minutes, and SQLite lets nobody else
write for the duration, so unrelated queries died with ``database is locked``
fifteen seconds in and the traceback named whichever innocent statement happened
to be next. (Reported against a Synology mount; the log blamed the WebSocket
token cleanup, which had nothing to do with it.)

Two rules make that impossible here, and they are the whole design:

1. **Nothing slow happens inside a session.** Walking, stat-ing, hashing and
   parsing are done first, into plain data. The session is opened afterwards,
   holds the lock for milliseconds, and closes.
2. **Nothing slow happens on the event loop.** Every blocking call goes through
   ``asyncio.to_thread``. Short transactions alone would not have stopped the
   WebSocket dropping — what stalled the loop was the network and the parser.

⚠️ **The helpers still live in ``api/routes/library.py``** and are imported
lazily, inside the functions that use them, because that module imports this one.
They are pure functions that belong in a service; moving them means touching a
3800-line route in eight places, which does not belong in a bug fix. Recorded as
debt rather than done quietly.
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
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_scan import LibraryScanJob
from backend.app.services.product_sync import resync_file_products

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


# ── The walk ─────────────────────────────────────────────────────────────────


def _walk_sync(root: Path, show_hidden: bool) -> list[tuple[str, list[str]]]:
    """Every directory under ``root`` with the files in it.

    ⚠️ Runs in a thread. On a network share each ``readdir`` is a round trip, and
    on the event loop that stalls every other request in the process — which is
    what made the WebSocket drop, which made the frontend ask for a token, which
    is the request the user saw fail.
    """
    out: list[tuple[str, list[str]]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not show_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            filenames = [f for f in filenames if not f.startswith(".")]
        out.append((dirpath, filenames))
    return out


async def collect_tree(root: Path, show_hidden: bool) -> list[tuple[str, list[str]]]:
    """The whole tree up front, off the loop.

    Collected rather than streamed for a second reason beyond not blocking: it
    is what gives the job a total, and progress that is a fraction beats a
    number rising toward nothing in particular.
    """
    return await asyncio.to_thread(_walk_sync, root, show_hidden)


# ── Examining one file, with no session in hand ──────────────────────────────


def _prepare_sync(
    dirpath: str,
    filename: str,
    root: Path,
    known: _Known | None,
    folder_rel: str,
) -> _Prepared | None:
    """Everything about a file that can be learned without the database.

    ⚠️ Runs in a thread and opens no session. The hash reads the whole file over
    the network and the 3MF parser unzips it — doing either with a transaction
    open is the bug this module exists to fix.
    """
    from backend.app.api.routes.library import (
        _SCANNABLE_EXTENSIONS,
        IMAGE_EXTENSIONS,
        _clean_3mf_metadata,
        _mtime_to_utc,
        calculate_file_hash,
        create_image_thumbnail,
        extract_gcode_thumbnail,
        get_library_thumbnails_dir,
        to_relative_path,
    )
    from backend.app.services.archive import ThreeMFParser
    from backend.app.services.library_helpers import detect_file_type
    from backend.app.services.library_ingest import external_hash_is_stale

    filepath = (
        Path(dirpath) / filename
    )  # SEC-PATH-OK: dirpath+filename come from os.walk of the folder root, not from a request
    ext = filepath.suffix.lower()
    if ext not in _SCANNABLE_EXTENSIONS:
        compound = "".join(filepath.suffixes[-2:]).lower() if len(filepath.suffixes) >= 2 else ""
        if compound not in _SCANNABLE_EXTENSIONS:
            return None

    # A symlink that leaves the mount is not part of this folder.
    try:
        filepath.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None

    try:
        stat = filepath.stat()
    except OSError:
        return None
    fs_modified_at = _mtime_to_utc(stat.st_mtime)
    file_path_str = str(filepath)

    if known is not None:
        # ⚠️ Re-hash only what moved. `external_hash_is_stale` answers off the
        # size and mtime already stored, so a mount that has not changed costs
        # no reads — which is what makes hashing mounts affordable at all.
        stale = external_hash_is_stale(
            SimpleNamespace(
                file_hash=known.file_hash,
                file_size=known.file_size,
                fs_modified_at=known.fs_modified_at,
            ),
            size=stat.st_size,
            mtime=fs_modified_at,
        )
        new_hash = None
        if stale:
            with contextlib.suppress(OSError):
                new_hash = calculate_file_hash(filepath)
        return _Prepared(
            path=filepath,
            file_path_str=file_path_str,
            filename=filename,
            folder_rel=folder_rel,
            size=stat.st_size,
            fs_modified_at=fs_modified_at,
            intent="refresh",
            known_id=known.id,
            new_hash=new_hash,
            mtime_changed=known.fs_modified_at != fs_modified_at,
        )

    file_type = detect_file_type(filepath.name)
    is_3mf_container = filepath.name.lower().endswith(".3mf")
    thumbnail_path = None
    file_metadata = None

    if is_3mf_container:
        try:
            parser = ThreeMFParser(str(filepath))
            meta = parser.parse()
            if meta:
                file_metadata = _clean_3mf_metadata(meta)
            thumb_data = parser.extract_thumbnail()
            if thumb_data:
                thumb_full = get_library_thumbnails_dir() / f"{uuid.uuid4().hex}.png"
                thumb_full.write_bytes(thumb_data)
                thumbnail_path = to_relative_path(thumb_full)
        except Exception:
            logger.debug("3MF parse failed during scan for %s", filepath, exc_info=True)

    if file_type == "gcode" and not is_3mf_container and thumbnail_path is None:
        thumb_data = extract_gcode_thumbnail(filepath)
        if thumb_data:
            thumb_full = get_library_thumbnails_dir() / f"{uuid.uuid4().hex}.png"
            thumb_full.write_bytes(thumb_data)
            thumbnail_path = to_relative_path(thumb_full)

    if ext in IMAGE_EXTENSIONS and thumbnail_path is None:
        made = create_image_thumbnail(filepath, get_library_thumbnails_dir())
        if made:
            thumbnail_path = to_relative_path(Path(made))

    try:
        content_hash = calculate_file_hash(filepath)
    except OSError:
        return None

    return _Prepared(
        path=filepath,
        file_path_str=file_path_str,
        filename=filename,
        folder_rel=folder_rel,
        size=stat.st_size,
        fs_modified_at=fs_modified_at,
        intent="create",
        file_type=file_type,
        content_hash=content_hash,
        thumbnail_path=thumbnail_path,
        file_metadata=file_metadata,
    )


async def prepare(dirpath: str, filename: str, root: Path, known: _Known | None, folder_rel: str) -> _Prepared | None:
    return await asyncio.to_thread(_prepare_sync, dirpath, filename, root, known, folder_rel)


# ── Writing a batch, with the session held for as little as possible ─────────


async def write_batch(
    batch: list[_Prepared],
    folder_ids: dict[str, int],
    counters: dict[str, int],
) -> list[tuple[int, str]]:
    """Persist one batch and return the rows that were created.

    ⚠️ The session is opened here and closed on the way out. Everything this
    needs was worked out before it was called, so the write lock is held for the
    length of a few INSERTs rather than the length of a scan.
    """
    from backend.app.api.routes.library import _without_print_name
    from backend.app.core.database import async_session
    from backend.app.services.library_helpers import skip_objects_supported_from_metadata, sync_system_tags
    from backend.app.services.library_ingest import find_reusable_row

    created: list[tuple[int, str]] = []
    #: Ids of the rows this batch actually rewrote — the ones whose bytes moved
    #: on disk, so a product that owns plates off them has to be reconciled.
    refreshed: list[int] = []

    async with async_session() as db:
        for item in batch:
            if item.intent == "refresh":
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
                if values:
                    await db.execute(update(LibraryFile).where(LibraryFile.id == item.known_id).values(**values))
                    counters["files_updated"] += 1
                    if item.known_id is not None:
                        refreshed.append(item.known_id)
                continue

            reusable = await find_reusable_row(db, content_hash=item.content_hash or "")
            if reusable is not None and reusable[1]:
                # The library already holds these bytes. Counted rather than
                # silent: a scan is also how people browse a mount, and a
                # skipped file reads as a scan that missed something.
                counters["skipped_duplicates"] += 1
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
            db.add(db_file)
            await db.flush()
            await sync_system_tags(db, db_file)
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
        # ⚠️ It reconciles against whatever ``file_metadata`` the ROW holds. A
        # refresh re-hashes but does not re-parse the 3MF (``_prepare_sync``
        # returns before the parser for a known file), so today this catches a
        # link that changed, not a plate list that did. The day the refresh
        # branch starts rewriting metadata, this hook is already in place.
        for library_file_id in refreshed:
            await resync_file_products(db, library_file_id)

        await db.commit()

    return created


async def ensure_folders(
    db: AsyncSession,
    root: Path,
    root_folder: LibraryFolder,
    directories: list[str],
    folder_ids: dict[str, int],
    counters: dict[str, int],
) -> None:
    """Create the subfolder rows the walk turned up.

    Done before the files and in its own short transactions: a file row needs a
    folder id, and discovering that mid-batch is what tied the old scan's
    folder writes to its file writes.
    """
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

    doomed = [
        (entry.id, path)
        for path, entry in known.items()
        if path not in found_paths and not await asyncio.to_thread(os.path.exists, path)
    ]
    if not doomed:
        return False

    for start in range(0, len(doomed), BATCH_SIZE):
        chunk = doomed[start : start + BATCH_SIZE]
        async with async_session() as db:
            rows = (
                (await db.execute(select(LibraryFile).where(LibraryFile.id.in_([i for i, _ in chunk])))).scalars().all()
            )
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
    async with async_session() as db:
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
            if not sub.external_path or await asyncio.to_thread(os.path.exists, sub.external_path):
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


async def _set_job(job_id: int, **values) -> None:
    """Write a few columns of the job and let go of the session immediately."""
    from backend.app.core.database import async_session

    async with async_session() as db:
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


async def run_scan(job_id: int) -> None:
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

        await _set_job(job_id, status="running", started_at=_now())

        # ⚠️ Asked in a thread. On an unreachable mount this call itself blocks,
        # and on the loop it would stall the process before the scan even began.
        reachable = await asyncio.to_thread(lambda: root.exists() and root.is_dir())
        if not reachable:
            await _fail_job(job_id, folder_id, f"external path is not accessible: {root}")
            return

        tree = await collect_tree(root, show_hidden)
        total = sum(len(files) for _, files in tree)
        await _set_job(job_id, files_total=total)

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
            for row_id, path, digest, size, mtime in (
                await db.execute(
                    select(
                        LibraryFile.id,
                        LibraryFile.file_path,
                        LibraryFile.file_hash,
                        LibraryFile.file_size,
                        LibraryFile.fs_modified_at,
                    ).where(LibraryFile.folder_id.in_(list(folder_ids.values())))
                )
            ).all():
                known[path] = _Known(id=row_id, file_hash=digest, file_size=size, fs_modified_at=mtime)

            await ensure_folders(db, root, folder, [d for d, _ in tree], folder_ids, counters)
            await db.commit()

        found_paths: set[str] = set()
        batch: list[_Prepared] = []
        last_progress = 0.0

        async def flush(force: bool = False) -> None:
            nonlocal batch, last_progress
            if batch and (force or len(batch) >= BATCH_SIZE):
                created = await write_batch(batch, folder_ids, counters)
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

        for dirpath, filenames in tree:
            rel = str(Path(dirpath).relative_to(root)).replace("\\", "/")
            if rel == ".":
                rel = ""
            for filename in filenames:
                counters["files_seen"] += 1
                candidate_path = str(
                    Path(dirpath) / filename
                )  # SEC-PATH-OK: both parts come from the walk of the folder root
                prepared = await prepare(dirpath, filename, root, known.get(candidate_path), rel)
                if prepared is None:
                    continue
                found_paths.add(prepared.file_path_str)
                batch.append(prepared)
                await flush()

        await flush(force=True)

        skipped = await remove_vanished(found_paths, known, folder_ids, counters)
        if skipped_duplicates:
            logger.info("scan skipped %d file(s) the library already holds", skipped_duplicates)

        await _set_job(job_id, status="finished", finished_at=_now(), skipped_deletions=skipped, **counters)
        await _announce_finish(
            {
                "job_id": job_id,
                "folder_id": folder_id,
                "status": "finished",
                "skipped_deletions": skipped,
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
        await _fail_job(job_id, folder_id, str(error))
    finally:
        _running.pop(job_id, None)


async def start_scan(folder_id: int, user_id: int | None) -> int:
    """Create the job row and set the worker going. Returns the job id."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        job = LibraryScanJob(folder_id=folder_id, status="queued", created_by=user_id)
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    _running[job_id] = asyncio.create_task(run_scan(job_id))
    return job_id


async def active_job_id(db: AsyncSession, folder_id: int) -> int | None:
    """The scan already running for this folder, if there is one.

    ⚠️ Two walks writing the same rows is not twice as fast, it is a race — the
    second would keep finding half-written state from the first.
    """
    row = (
        await db.execute(
            select(LibraryScanJob.id)
            .where(LibraryScanJob.folder_id == folder_id, LibraryScanJob.status.in_(("queued", "running")))
            .limit(1)
        )
    ).first()
    return row[0] if row else None


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
