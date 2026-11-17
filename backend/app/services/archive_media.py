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

⚠️ **Whoever starts the work captures what it starts from, before its first long
I/O**: the archive's incarnation, the storage epoch and the video it shows
(:func:`primary_token` — "no video" is a state too). ``publish_primary`` has no
default for that token. A manual door also hands in the caller's frozen
authority, and the seam asks the lifetime guard and the caller's rights again
inside the publication transaction — an account disabled while its upload was
being read publishes nothing. The scan after a print hands in its run proof,
asked once more immediately before the bytes take their name.

⚠️ **A printer recording's claim outlives the video made from it.** Replacing a
video read off a printer — the AVI→MP4 conversion, a layer video, an upload, a
reprint — leaves a ``receipt`` row holding the claim (and the hash of the bytes
that were downloaded, never the derived file's), so the recording cannot be
attached to another archive meanwhile. Only the operator's own removal of the
video, and the archive's deletion, release it.

⚠️ **"No video" has a revision too.** A removal leaves a ``retired`` row — a
tombstone carrying the next revision — so the token of an empty archive is
``none:<highest revision>``: an upload that captured the empty state, and then
slept through "a video was attached, and removed again", does not find the
state it started from.

⚠️ **A receipt is history, not a copy.** What may be deleted from a printer is
decided by a LIVE copy — the primary row that claims the recording, shown by
its archive, its file on disk — pinned for as long as the tidy-up runs
(:func:`pin_live_copy`): replacing or removing that video revokes the pin, and
a revoked pin deletes nothing from the printer.

⚠️ **A stored path authorises no unlink outside the data directory.** A pointer
from an import or an old writer is resolved and contained before anything is
removed; one that escapes is left alone and logged.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive_media_asset import (
    ORIGIN_LEGACY,
    ROLE_PRIMARY,
    ROLE_RECEIPT,
    ROLE_RETIRED,
    ArchiveMediaAsset,
)

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
    # The files of these archives are about to go: no tidy-up may delete a
    # printer's original on the strength of a copy of theirs.
    for pin in _pins:
        if pin.archive_id in ids:
            pin.revoked = True
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
#: The print this work was started for is no longer the printer's current one.
REFUSED_RUN_CHANGED = "run_changed"


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


#: The token of an archive that never had a video.
NO_VIDEO = "none:0"


def _token(primary: ArchiveMediaAsset | None, pointer: str | None, highest: int) -> str:
    """The state a caller compares: which video the archive shows, and which publication made it so.

    The pointer alone is not enough — a video removed and one of the same name
    attached again would look unchanged. A pointer no row describes yet (a
    video from before the seam) is its own state, ``legacy``. And "no video" is
    a state with a revision: ``highest`` is the highest revision the archive's
    rows carry, kept up by the tombstone a removal leaves, so the emptiness
    after "attached, then removed" is not the emptiness before it.
    """
    shown = (pointer or "").strip()
    if not shown:
        return f"none:{highest}"
    if primary is not None and primary.relative_path == shown:
        return f"{primary.revision}:{primary.asset_uuid}"
    return f"legacy:{shown}"


async def primary_token(db: AsyncSession, archive) -> str:
    """Capture the archive's current video, to hand to a later publication as ``expected_token``."""
    return _token(
        await primary_of(db, archive.id, archive.recovery_identity),
        archive.timelapse_path,
        await _next_revision(db, archive) - 1,
    )


async def _tombstone(db: AsyncSession, archive, revision: int, relative_path: str | None) -> None:
    """Leave the mark of a removal: one ``retired`` row per archive, carrying the removal's revision."""
    await db.execute(
        delete(ArchiveMediaAsset).where(
            ArchiveMediaAsset.archive_id == archive.id,
            ArchiveMediaAsset.archive_identity == archive.recovery_identity,
            ArchiveMediaAsset.role == ROLE_RETIRED,
        )
    )
    db.add(
        ArchiveMediaAsset(
            asset_uuid=str(uuid4()),
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            role=ROLE_RETIRED,
            revision=revision,
            relative_path=relative_path or "",
            origin=ORIGIN_LEGACY,
            created_at=_now(),
        )
    )
    await db.flush()


async def _reconcile(db: AsyncSession, archive, receipts: list | None = None) -> ArchiveMediaAsset | None:
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
    # Read before the stale row goes: a revision is never handed out twice.
    revision = await _next_revision(db, archive)
    if primary is not None:
        # Stale: an old writer replaced or removed the video after this row was written.
        # What it claimed on a printer stays claimed (the receipt is written by the caller).
        stale_path = primary.relative_path
        await _retire(db, primary, receipts)
        if not shown:
            await _tombstone(db, archive, revision, stale_path)
    if not shown:
        return None
    legacy = ArchiveMediaAsset(
        asset_uuid=str(uuid4()),
        archive_id=archive.id,
        archive_identity=archive.recovery_identity,
        role=ROLE_PRIMARY,
        revision=revision,
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


def _claim_of(row: ArchiveMediaAsset) -> tuple | None:
    """The printer recording a row claims, or ``None``."""
    if not row.remote_printer_identity or not row.remote_path:
        return None
    return (row.remote_printer_identity, row.remote_storage, row.remote_path, row.content_sha256)


async def _retire(db: AsyncSession, row: ArchiveMediaAsset, receipts: list | None = None) -> None:
    """Delete a primary row that is being replaced, noting in ``receipts`` the claim of a recording it held.

    The receipt itself is written by :func:`_keep_receipts` AFTER the row that
    replaces this one, so the two never hold the unique claim at once. Without
    a list the claim is released.
    """
    if receipts is not None and _claim_of(row) is not None:
        receipts.append(
            {
                "asset_uuid": row.asset_uuid,
                "archive_id": row.archive_id,
                "archive_identity": row.archive_identity,
                "revision": row.revision,
                "relative_path": row.relative_path,
                "size_bytes": row.size_bytes,
                "content_sha256": row.content_sha256,
                "origin": row.origin,
                "remote_printer_identity": row.remote_printer_identity,
                "remote_storage": row.remote_storage,
                "remote_path": row.remote_path,
                "remote_size": row.remote_size,
                "remote_mtime_raw": row.remote_mtime_raw,
                "created_at": row.created_at,
            }
        )
    # Before the commit, on purpose: a tidy-up must not delete from the
    # printer on the strength of a copy that is being taken away.
    _revoke(asset_uuid=row.asset_uuid)
    await db.delete(row)
    await db.flush()


async def _keep_receipts(db: AsyncSession, receipts: list, *, unless: tuple | None = None) -> None:
    """Write the receipts of the rows this transaction retired — except a claim the new row itself now holds."""
    for values in receipts:
        claim = (
            values["remote_printer_identity"],
            values["remote_storage"],
            values["remote_path"],
            values["content_sha256"],
        )
        if unless is not None and claim == unless:
            continue
        db.add(ArchiveMediaAsset(role=ROLE_RECEIPT, **values))
    await db.flush()


async def _drop_own_receipts(db: AsyncSession, archive, *, claim: tuple | None = None) -> None:
    """Forget this archive's receipts: all of them, or the one for ``claim`` (it is being claimed afresh)."""
    statement = delete(ArchiveMediaAsset).where(
        ArchiveMediaAsset.archive_id == archive.id,
        ArchiveMediaAsset.archive_identity == archive.recovery_identity,
        ArchiveMediaAsset.role == ROLE_RECEIPT,
    )
    if claim is not None:
        statement = statement.where(
            ArchiveMediaAsset.remote_printer_identity == claim[0],
            ArchiveMediaAsset.remote_storage == claim[1],
            ArchiveMediaAsset.remote_path == claim[2],
            ArchiveMediaAsset.content_sha256 == claim[3],
        )
    await db.execute(statement)


@dataclass
class CopyPin:
    """A live local copy of one printer recording, held while the tidy-up decides about the original.

    ``holds()`` is synchronous on purpose: it is asked on the printer's file
    lane — in a worker thread for FTP — immediately before the delete command.
    It asks two things: nobody took the copy away (every door that does —
    replace, remove, reprint reset, the hard delete of the archive — revokes the
    pin), and the file is on disk at this very moment. The second is the
    physical fact itself, so a door that forgot to revoke still deletes nothing
    from the printer.
    """

    asset_uuid: str
    archive_id: int
    relative_path: str
    content_sha256: str
    local: Path
    revoked: bool = False

    def holds(self) -> bool:
        try:
            return not self.revoked and self.local.is_file()
        except OSError:
            return False

    def matches(self, data: bytes | None) -> bool:
        """Whether ``data`` — the recording as it is on the printer NOW — is the very video this copy holds."""
        import hashlib  # noqa: PLC0415

        return bool(data) and hashlib.sha256(data).hexdigest() == self.content_sha256 and self.holds()


#: Pins of running tidy-ups. Process memory: this process is the only writer that can take the copy away.
_pins: list[CopyPin] = []


def _revoke(*, asset_uuid: str | None = None, relative_path: str | None = None) -> None:
    for pin in _pins:
        if (asset_uuid is not None and pin.asset_uuid == asset_uuid) or (
            relative_path is not None and pin.relative_path == relative_path
        ):
            pin.revoked = True


async def _shown_copy(db: AsyncSession, base_dir: Path, printer_identity: str, storage: str, remote_path: str):
    """``(row, local file)`` of the live copy as the database and the disk have it NOW, or ``None``."""
    from backend.app.models.archive import PrintArchive  # noqa: PLC0415

    # A statement of its own transaction: on SQLite a session that has already
    # read keeps its snapshot, and would not see a removal committed since.
    await db.rollback()
    found = (
        await db.execute(
            select(ArchiveMediaAsset, PrintArchive.timelapse_path)
            .join(PrintArchive, PrintArchive.id == ArchiveMediaAsset.archive_id)
            .where(
                ArchiveMediaAsset.role == ROLE_PRIMARY,
                ArchiveMediaAsset.remote_printer_identity == printer_identity,
                ArchiveMediaAsset.remote_storage == storage,
                ArchiveMediaAsset.remote_path == normalise_remote_path(remote_path),
                ArchiveMediaAsset.content_sha256.is_not(None),
                PrintArchive.recovery_identity == ArchiveMediaAsset.archive_identity,
            )
            .execution_options(populate_existing=True)
            .limit(1)
        )
    ).first()
    if found is None:
        return None
    row, pointer = found
    if (pointer or "").strip() != row.relative_path:
        return None
    local = await asyncio.to_thread(contained, base_dir, row.relative_path)
    if local is None or not await asyncio.to_thread(local.is_file):
        return None
    return row, local


async def pin_live_copy(
    db: AsyncSession, base_dir: Path, printer_identity: str, storage: str, remote_path: str
) -> CopyPin | None:
    """Pin the live copy of one printer recording, or ``None`` when no archive holds one.

    Live means: a PRIMARY row claims the recording, its archive's pointer names
    that row's file, and the file is on disk inside the data directory. A
    receipt is history — the video made from the recording was replaced, and
    its file may be gone — so it authorises nothing here.

    ⚠️ **The pin is registered first and the copy is looked at AGAIN after
    that.** Finding the copy takes awaits — the query, the disk — and a removal
    can win any of them; a pin registered on what was seen before would be a
    valid pin on a copy that is gone. So: look, register, look again. A removal
    that committed before the second look is seen by it; one that commits after
    it meets a registered pin and revokes it — in ``_retire`` / ``forget_archives``
    before its commit, and again by path immediately before its file goes
    (``_unlink``).
    """
    seen = await _shown_copy(db, base_dir, printer_identity, storage, remote_path)
    if seen is None:
        return None
    row, local = seen
    pin = CopyPin(row.asset_uuid, row.archive_id, row.relative_path, row.content_sha256, local)
    _pins.append(pin)
    try:
        again = await _shown_copy(db, base_dir, printer_identity, storage, remote_path)
    except BaseException:
        release_pin(pin)
        raise
    if again is None or again[0].asset_uuid != pin.asset_uuid or not pin.holds():
        release_pin(pin)
        return None
    return pin


def release_pin(pin: CopyPin | None) -> None:
    """End a pin: the tidy-up it served is over."""
    if pin is not None and pin in _pins:
        _pins.remove(pin)


def contained(base_dir: Path, relative_path: str | None) -> Path | None:
    """The file a stored relative path names, or ``None`` when it does not stay inside the data directory.

    A pointer can come from an import, a restore or a writer older than the
    seam; ``..`` segments, an absolute path, or a link or junction that leads
    out, authorise nothing.
    """
    from backend.app.utils.safe_path import PathTraversalError, safe_join_under  # noqa: PLC0415

    if not relative_path:
        return None
    try:
        return safe_join_under(base_dir, relative_path, http=False)
    except (PathTraversalError, OSError, ValueError):
        return None


async def _unlink(base_dir: Path, relative_path: str | None) -> None:
    if not relative_path:
        return
    # After the commit, before the file goes: a pin registered since the row was retired.
    _revoke(relative_path=relative_path)
    target = await asyncio.to_thread(contained, base_dir, relative_path)
    if target is None:
        logger.warning(
            "Archive media: the stored video path %r leaves the data directory — nothing is removed", relative_path
        )
        return
    try:
        await asyncio.to_thread(target.unlink, missing_ok=True)
    except OSError as exc:
        # Left for the prune script: the row and the pointer are already right.
        logger.warning("Archive media: could not remove the replaced video %s: %s", relative_path, exc)


async def unlink_released(db: AsyncSession, base_dir: Path, relative_path: str | None, archive_id: int) -> None:
    """Remove a video its archive no longer shows — AFTER the caller's commit, and only when nobody shows it.

    For the one writer that detaches inside a transaction of its own
    (:func:`detach_primary_in`).
    """
    if not relative_path or await _still_shown_elsewhere(db, relative_path, archive_id):
        return
    shown = (
        await db.execute(
            select(ArchiveMediaAsset.id)
            .where(ArchiveMediaAsset.relative_path == relative_path, ArchiveMediaAsset.role == ROLE_PRIMARY)
            .limit(1)
        )
    ).first()
    if shown is None:
        await _unlink(base_dir, relative_path)


async def _authorise(db: AsyncSession, authority, permissions: tuple) -> None:
    """The caller's lifetime guard and current rights, on the publication's own session.

    ``authority`` is the frozen capture a manual door took before its I/O;
    ``None`` is a server writer (the scan after a print, the conversion, the
    layer stitch). Raises the gate's own 401 / 403.
    """
    if authority is None:
        return
    from backend.app.services import domain_authority  # noqa: PLC0415

    await domain_authority.guard(db, authority, permissions=permissions)


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
    expected_token: str,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    remote: RemoteSource | None = None,
    authority=None,
    permissions: tuple = (),
    may_run: Callable[[], bool] | None = None,
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
    claim = (
        (remote.printer_identity, remote.storage, normalise_remote_path(remote.path), content_sha256)
        if remote
        else None
    )
    try:
        await db.commit()
        receipts: list = []
        async with (
            queue_sources.storage_mutation(),
            # The lifetime guard before the archive's rows, after SQLite's writer.
            archive_write_scope(db, archive_id, first=lambda: _authorise(db, authority, permissions)),
        ):
            current = await _load_target(db, archive_id, expected_identity, expected_epoch)
            if current is None:
                await db.rollback()
                return Publication(False, REFUSED_TARGET_GONE)
            if await primary_token(db, current) != expected_token:
                await db.rollback()
                return Publication(False, REFUSED_SOURCE_CHANGED)
            if may_run is not None and not may_run():
                await db.rollback()
                return Publication(False, REFUSED_RUN_CHANGED)
            old = await _reconcile(db, current, receipts)
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
                        await _retire(db, old, receipts)
                    if claim is not None:
                        # The same recording attached to the same archive again: its own receipt gives way.
                        await _drop_own_receipts(db, current, claim=claim)
                    db.add(row)
                    await db.flush()
                    # After the new row: what the replaced video claimed on a printer stays claimed.
                    await _keep_receipts(db, receipts, unless=claim)
            except IntegrityError as exc:
                if "remote" not in str(exc.orig):
                    raise
                # Another archive holds this recording. Nobody waited on its lock: the index decided.
                await db.rollback()
                return Publication(False, REFUSED_SOURCE_CLAIMED)
            if replaced and (replaced == relative or await _still_shown_elsewhere(db, replaced, current.id)):
                replaced = None
            asset_uuid = row.asset_uuid
            if may_run is not None and not may_run():
                # The printer moved on while this waited for its locks: nothing is published.
                await db.rollback()
                return Publication(False, REFUSED_RUN_CHANGED)
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
    authority=None,
    permissions: tuple = (),
) -> Publication:
    """Take the archive's video away: the row and the pointer in one transaction, the file after the commit.

    ``expected_token`` ``None`` removes whatever the archive shows now — a
    removal cannot bring an older video back. The operator's removal also
    releases what the archive claimed on a printer: the recording may then be
    attached where it belongs.
    """
    from backend.app.services import queue_sources  # noqa: PLC0415
    from backend.app.services.archive_write_scope import archive_write_scope  # noqa: PLC0415

    await db.commit()
    async with (
        queue_sources.storage_mutation(),
        archive_write_scope(db, archive_id, first=lambda: _authorise(db, authority, permissions)),
    ):
        current = await _load_target(db, archive_id, expected_identity, expected_epoch)
        if current is None:
            await db.rollback()
            return Publication(False, REFUSED_TARGET_GONE)
        if expected_token is not None and await primary_token(db, current) != expected_token:
            await db.rollback()
            return Publication(False, REFUSED_SOURCE_CHANGED)
        old = await _reconcile(db, current)
        removed = (current.timelapse_path or "").strip() or None
        if old is not None:
            # The emptiness this leaves is a new state: its revision is kept by a tombstone.
            revision = await _next_revision(db, current)
            await _retire(db, old)
            await _tombstone(db, current, revision, removed)
        await _drop_own_receipts(db, current)
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
    of the row. The caller commits, and only then hands the path this returns to
    :func:`unlink_released`.
    """
    receipts: list = []
    await _reconcile(db, archive, receipts)
    shown = (archive.timelapse_path or "").strip() or None
    primary = await primary_of(db, archive.id, archive.recovery_identity)
    if primary is not None:
        revision = await _next_revision(db, archive)
        await _retire(db, primary, receipts)
        await _tombstone(db, archive, revision, shown)
    # The earlier run's recording stays claimed by this archive.
    await _keep_receipts(db, receipts)
    archive.timelapse_path = None
    return shown
