"""The videos of print archives — the registry's only writer and the one publication seam (G10 C, m237).

``archive_media_assets`` holds one row per video published to an archive.
``print_archives.timelapse_path`` remains what readers follow; it is the
projection of the archive's ``primary`` row, and the two are written in ONE
transaction, here.

⚠️ **Only this module writes the table, and only this module writes
``timelapse_path``** (``tests/unit/test_archive_media_has_one_writer.py``).
Every door that gives an archive a video, replaces it or removes it — the scan
after a print, the manual scan and pick, an upload, the AVI conversion, the
layer stitch, the delete button, a reprint — goes through
:func:`publish_primary` / :func:`remove_primary`:

* the bytes are complete in a staged file before anything is decided, and they
  never overwrite the file readers are following;
* under the storage guard and the archive's write scope the archive is read
  again: same incarnation, same storage epoch, still current — and, when the
  caller captured it, the same video as when the caller started
  (:func:`primary_token`; "no video" is a state too). A slow download or
  conversion that finishes after somebody replaced the video is refused
  ``source_changed``; it does not win by being last;
* a recording read off a printer is claimed: the printer's immutable identity,
  the medium, the path and the SHA-256 of the bytes. The unique index decides a
  race between two archives — one wins, the other is refused ``source_claimed``;
* the row and the pointer commit together; the replaced file is removed only
  after that commit, and never while another archive still points at it.

⚠️ **A row is bound to the archive's ``recovery_identity``, not to its id.**
SQLite hands a deleted archive's id to the next one, so every read here compares
the identity and a row left under a reused id answers nothing.

⚠️ **Rows from before the seam are reconciled, never trusted over the pointer**
(review N1). m237 described the videos that existed then; until this seam every
old writer could still replace or remove a video without touching its row. So a
primary row whose path is not the pointer's is stale: it is dropped, the
pointer is described afresh as ``legacy`` — no source is guessed for it — and a
video whose pointer is gone is not brought back.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive_media_asset import ORIGIN_LEGACY, ROLE_PRIMARY, ArchiveMediaAsset

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


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------

#: The archive is gone, is another incarnation, is no longer current, or the storage was restored.
REFUSED_TARGET_GONE = "target_gone"
#: The archive's video is not the one the caller started from.
REFUSED_SOURCE_CHANGED = "source_changed"
#: This recording already belongs to another archive.
REFUSED_SOURCE_CLAIMED = "source_claimed"
#: A restore or shutdown has closed admission (``services/archive_media_runtime``).
REFUSED_RESTORING = "restoring"


@dataclass(frozen=True)
class RemoteSource:
    """Where on a printer a recording was read from. ``printer_identity`` is ``Printer.connector_identity``."""

    printer_identity: str
    storage: str
    path: str
    size: int | None = None
    mtime_raw: str | None = None


@dataclass(frozen=True)
class Publication:
    """What a publication or removal did. ``refusal`` is one of the ``REFUSED_*`` codes, else ``None``."""

    ok: bool
    refusal: str | None = None
    relative_path: str | None = None
    revision: int | None = None
    asset_uuid: str | None = None


def normalise_remote_path(path: str) -> str:
    """One spelling per remote file: forward slashes, one leading slash, no doubled or trailing ones."""
    parts = [part for part in (path or "").replace("\\", "/").split("/") if part]
    return "/" + "/".join(parts)


def _token(primary: ArchiveMediaAsset | None, pointer: str | None) -> str:
    """The state a caller compares: which video the archive shows, and which publication made it so.

    The pointer alone is not enough — a video removed and one of the same name
    attached again would look unchanged. A pointer no row describes yet (a
    video from before the seam) is its own state, ``legacy``.
    """
    shown = (pointer or "").strip()
    if not shown:
        return "none"
    if primary is not None and primary.relative_path == shown:
        return f"{primary.revision}:{primary.asset_uuid}"
    return f"legacy:{shown}"


async def primary_token(db: AsyncSession, archive) -> str:
    """Capture the archive's current video, to hand to a later publication as ``expected_token``."""
    return _token(await primary_of(db, archive.id, archive.recovery_identity), archive.timelapse_path)


async def _reconcile(db: AsyncSession, archive) -> ArchiveMediaAsset | None:
    """Make the registry agree with the pointer, under the archive's lock. Returns the primary row or ``None``.

    The pointer is what the archive shows; rows are descriptions of it. See the
    module docstring: nothing is brought back, nothing newer is overwritten and
    no source is invented.
    """
    # Rows of an earlier archive that had this id: nobody's now.
    await db.execute(
        delete(ArchiveMediaAsset).where(
            ArchiveMediaAsset.archive_id == archive.id,
            ArchiveMediaAsset.archive_identity != archive.recovery_identity,
        )
    )
    primary = await primary_of(db, archive.id, archive.recovery_identity)
    shown = (archive.timelapse_path or "").strip()
    if primary is not None and primary.relative_path == shown:
        return primary
    if primary is not None:
        # Stale: an old writer replaced or removed the video after this row was written.
        await db.delete(primary)
        await db.flush()
    if not shown:
        return None
    legacy = ArchiveMediaAsset(
        asset_uuid=str(uuid4()),
        archive_id=archive.id,
        archive_identity=archive.recovery_identity,
        role=ROLE_PRIMARY,
        revision=await _next_revision(db, archive),
        relative_path=shown,
        origin=ORIGIN_LEGACY,
        created_at=_now(),
    )
    db.add(legacy)
    await db.flush()
    return legacy


async def _next_revision(db: AsyncSession, archive) -> int:
    highest = (
        await db.execute(
            select(func.max(ArchiveMediaAsset.revision)).where(
                ArchiveMediaAsset.archive_id == archive.id,
                ArchiveMediaAsset.archive_identity == archive.recovery_identity,
            )
        )
    ).scalar()
    return int(highest or 0) + 1


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _still_shown_elsewhere(db: AsyncSession, relative_path: str, archive_id: int) -> bool:
    """Whether another archive still points at this file — a 3MF folder can be shared by content dedup."""
    from backend.app.models.archive import PrintArchive  # noqa: PLC0415

    other = await db.execute(
        select(PrintArchive.id)
        .where(PrintArchive.timelapse_path == relative_path, PrintArchive.id != archive_id)
        .limit(1)
    )
    return other.first() is not None


def _free_name(directory: Path, filename: str) -> Path:
    """A name in ``directory`` nothing uses: a publication never writes over a file somebody may be reading."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    return directory / f"{stem}.{uuid4().hex[:8]}{suffix}"


async def _load_target(db: AsyncSession, archive_id: int, expected_identity: str, expected_epoch: int):
    from backend.app.services import queue_sources  # noqa: PLC0415
    from backend.app.services.archive_write_scope import load_active_archive_for_write  # noqa: PLC0415
    from backend.app.services.print_execution import NONCURRENT  # noqa: PLC0415

    current = await load_active_archive_for_write(db, archive_id)
    if (
        current is None
        or current.recovery_identity != expected_identity
        or queue_sources.current_epoch() != expected_epoch
        or NONCURRENT in (current.extra_data or {})
    ):
        return None
    return current


async def _unlink(base_dir: Path, relative_path: str | None) -> None:
    if not relative_path:
        return
    try:
        await asyncio.to_thread((base_dir / relative_path).unlink, missing_ok=True)
    except OSError as exc:
        # Left for the prune script: the row and the pointer are already right.
        logger.warning("Archive media: could not remove the replaced video %s: %s", relative_path, exc)


async def publish_primary(db: AsyncSession, archive_id: int, *, staged: Path, **kwargs) -> Publication:
    """Make ``staged`` the archive's video, or refuse and leave everything as it was.

    An operation the media runtime owns (G10 C4): refused while a restore holds
    admission, and not detached by a cancelled caller. See :func:`_publish_primary`.
    """
    from backend.app.services.archive_media_runtime import (  # noqa: PLC0415
        MediaAdmissionClosed,
        archive_media_runtime,
    )

    try:
        return await archive_media_runtime.own(
            f"publish:{archive_id}", _publish_primary(db, archive_id, staged=staged, **kwargs)
        )
    except MediaAdmissionClosed:
        await asyncio.to_thread(staged.unlink, missing_ok=True)
        return Publication(False, REFUSED_RESTORING)


async def remove_primary(db: AsyncSession, archive_id: int, **kwargs) -> Publication:
    """Take the archive's video away — an operation the media runtime owns. See :func:`_remove_primary`."""
    from backend.app.services.archive_media_runtime import (  # noqa: PLC0415
        MediaAdmissionClosed,
        archive_media_runtime,
    )

    try:
        return await archive_media_runtime.own(f"remove:{archive_id}", _remove_primary(db, archive_id, **kwargs))
    except MediaAdmissionClosed:
        return Publication(False, REFUSED_RESTORING)


async def _publish_primary(
    db: AsyncSession,
    archive_id: int,
    *,
    staged: Path,
    directory: Path,
    filename: str,
    base_dir: Path,
    origin: str,
    expected_identity: str,
    expected_epoch: int,
    expected_token: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    remote: RemoteSource | None = None,
) -> Publication:
    """Make ``staged`` the archive's video, or refuse and leave everything as it was.

    ``staged`` is a complete file on the data volume; it is moved, never copied
    over a live file. On any refusal or error it is removed — it is this
    publication's own output. The replaced video is unlinked after the commit.
    """
    from backend.app.services import queue_sources  # noqa: PLC0415
    from backend.app.services.archive_write_scope import archive_write_scope  # noqa: PLC0415

    final: Path | None = None
    replaced: str | None = None
    committed = False
    try:
        await db.commit()
        async with queue_sources.storage_mutation(), archive_write_scope(db, archive_id):
            current = await _load_target(db, archive_id, expected_identity, expected_epoch)
            if current is None:
                await db.rollback()
                return Publication(False, REFUSED_TARGET_GONE)
            if expected_token is not None:
                shown = await primary_of(db, current.id, current.recovery_identity)
                if _token(shown, current.timelapse_path) != expected_token:
                    await db.rollback()
                    return Publication(False, REFUSED_SOURCE_CHANGED)
            old = await _reconcile(db, current)
            revision = await _next_revision(db, current)
            target = await asyncio.to_thread(_free_name, directory, filename)
            relative = str(target.relative_to(base_dir))
            row = ArchiveMediaAsset(
                asset_uuid=str(uuid4()),
                archive_id=current.id,
                archive_identity=current.recovery_identity,
                role=ROLE_PRIMARY,
                revision=revision,
                relative_path=relative,
                size_bytes=size_bytes,
                content_sha256=content_sha256,
                origin=origin,
                remote_printer_identity=remote.printer_identity if remote else None,
                remote_storage=remote.storage if remote else None,
                remote_path=normalise_remote_path(remote.path) if remote else None,
                remote_size=remote.size if remote else None,
                remote_mtime_raw=remote.mtime_raw if remote else None,
                created_at=_now(),
            )
            try:
                async with db.begin_nested():
                    if old is not None:
                        replaced = old.relative_path
                        await db.delete(old)
                        await db.flush()
                    db.add(row)
                    await db.flush()
            except IntegrityError as exc:
                if "remote" not in str(exc.orig):
                    raise
                # Another archive holds this recording. Nobody waited on its lock: the index decided.
                await db.rollback()
                return Publication(False, REFUSED_SOURCE_CLAIMED)
            if replaced and (replaced == relative or await _still_shown_elsewhere(db, replaced, current.id)):
                replaced = None
            asset_uuid = row.asset_uuid
            # The bytes take their name only now, after every check, and before
            # the commit — so the pointer never names a file that is not there.
            await asyncio.to_thread(os.replace, staged, target)
            final = target
            current.timelapse_path = relative
            await db.commit()
            committed = True
    finally:
        # Whatever was not published is this publication's own to remove; a
        # cancelled caller still waits for that.
        if not committed and final is not None:
            await asyncio.shield(asyncio.to_thread(final.unlink, missing_ok=True))
        await asyncio.shield(asyncio.to_thread(staged.unlink, missing_ok=True))
    await _unlink(base_dir, replaced)
    return Publication(True, None, relative, revision, asset_uuid)


async def _remove_primary(
    db: AsyncSession,
    archive_id: int,
    *,
    base_dir: Path,
    expected_identity: str,
    expected_epoch: int,
    expected_token: str | None = None,
) -> Publication:
    """Take the archive's video away: the row and the pointer in one transaction, the file after the commit."""
    from backend.app.services import queue_sources  # noqa: PLC0415
    from backend.app.services.archive_write_scope import archive_write_scope  # noqa: PLC0415

    await db.commit()
    async with queue_sources.storage_mutation(), archive_write_scope(db, archive_id):
        current = await _load_target(db, archive_id, expected_identity, expected_epoch)
        if current is None:
            await db.rollback()
            return Publication(False, REFUSED_TARGET_GONE)
        if expected_token is not None:
            shown = await primary_of(db, current.id, current.recovery_identity)
            if _token(shown, current.timelapse_path) != expected_token:
                await db.rollback()
                return Publication(False, REFUSED_SOURCE_CHANGED)
        old = await _reconcile(db, current)
        removed = (current.timelapse_path or "").strip() or None
        if old is not None:
            await db.delete(old)
        if removed and await _still_shown_elsewhere(db, removed, current.id):
            removed = None
        current.timelapse_path = None
        await db.commit()
    await _unlink(base_dir, removed)
    return Publication(True)


async def detach_primary_in(db: AsyncSession, archive) -> str | None:
    """Take the video off an archive INSIDE the caller's transaction. Never commits, removes no file.

    For a writer that is already changing the archive row in one transaction
    of its own — a reprint that reuses the row resets its video with the rest
    of the row. The caller commits, and removes the file this returns.
    """
    await _reconcile(db, archive)
    shown = (archive.timelapse_path or "").strip() or None
    primary = await primary_of(db, archive.id, archive.recovery_identity)
    if primary is not None:
        await db.delete(primary)
    archive.timelapse_path = None
    return shown
