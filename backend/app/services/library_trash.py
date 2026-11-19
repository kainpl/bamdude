"""Library trash sweeper + purge service (#1008).

Two-stage file deletion for the library:

1. Users / admins soft-delete files — the row stays in ``library_files`` with
   ``deleted_at`` stamped; the bytes stay on disk. This is handled inline in
   ``backend.app.api.routes.library`` and exposed to admins as a bulk "purge
   old files" operation via :meth:`LibraryTrashService.purge_older_than`.

2. A background sweeper in this service hard-deletes rows (and their bytes)
   whose ``deleted_at`` is older than the configured retention window.

External files (``is_external=True``) are never placed in the trash — their
bytes live outside BamDude's control, so there's nothing to restore.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote
from backend.app.models.settings import Settings
from backend.app.services import part_images, queue_source_release

logger = logging.getLogger(__name__)

# Settings key used to persist the trash retention window (days). The sweeper
# reads this on every tick so the UI can change it without a restart.
TRASH_RETENTION_KEY = "library_trash_retention_days"
DEFAULT_RETENTION_DAYS = 30
# Clamp retention to a sensible range. 1 day is a reasonable floor (anything
# shorter just makes trash into hard-delete); 365 gives admins plenty of rope
# without letting accidental typos (99999) grow the table unboundedly.
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365

# Auto-purge settings (#1008 follow-up). When enabled, the sweeper loop also
# runs the admin bulk purge once per 24h using the saved age threshold.
# Default-off so existing installs don't surprise users — opt-in via Settings.
AUTO_PURGE_ENABLED_KEY = "library_auto_purge_enabled"
AUTO_PURGE_DAYS_KEY = "library_auto_purge_days"
AUTO_PURGE_INCLUDE_NEVER_PRINTED_KEY = "library_auto_purge_include_never_printed"
AUTO_PURGE_LAST_RUN_KEY = "library_auto_purge_last_run"
# Persist the result count alongside the timestamp so a server restart between
# the auto-tick and the next status read doesn't downgrade the UI to "count
# was lost". 0 is a valid count ("ran, found nothing") and is shown as such.
AUTO_PURGE_LAST_MOVED_KEY = "library_auto_purge_last_moved"
DEFAULT_AUTO_PURGE_DAYS = 90
MIN_AUTO_PURGE_DAYS = 7  # anything shorter is begging for accidents
MAX_AUTO_PURGE_DAYS = 3650


def _age_cutoff(now: datetime, older_than_days: int) -> datetime:
    return now - timedelta(days=older_than_days)


def _purge_filter(cutoff: datetime, include_never_printed: bool):
    """SQLAlchemy clause selecting files eligible for admin purge.

    A file is "old" if either (a) ``last_printed_at`` is set and predates the
    cutoff, or (b) ``last_printed_at`` is NULL *and* the file was uploaded
    before the cutoff — but only when ``include_never_printed`` is True.
    """
    last_printed_old = and_(
        LibraryFile.last_printed_at.isnot(None),
        LibraryFile.last_printed_at < cutoff,
    )
    if include_never_printed:
        never_printed_old = and_(
            LibraryFile.last_printed_at.is_(None),
            LibraryFile.created_at < cutoff,
        )
        age_clause = or_(last_printed_old, never_printed_old)
    else:
        age_clause = last_printed_old
    return and_(
        LibraryFile.deleted_at.is_(None),
        LibraryFile.is_external.is_(False),
        age_clause,
    )


@dataclass(slots=True)
class LibraryPurgeRunResult:
    """Outcome of one library auto-purge tick.

    Mirrors the shape of ``archive_cleanup_service.CleanupRunResult`` so the
    settings UI can render "last run" / "next run" cards uniformly across
    the two bins. ``moved`` is the count of files that were stamped with
    ``deleted_at`` (sent to trash); ``files_purged`` is reserved for a
    future hard-delete telemetry add — kept zero today since trash sweep
    runs on the same loop and its count is logged separately.
    """

    started_at: datetime
    finished_at: datetime | None = None
    moved: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "moved": self.moved,
        }


async def forget_file_notes(db: AsyncSession, library_file_ids) -> None:
    """These library files are being purged with a bulk DELETE: their notes go first.

    ``library_file_notes.library_file_id`` is ``ON DELETE CASCADE``, which SQLite
    never runs, and a Core ``delete(LibraryFile)`` goes past the ORM cascade that
    covers ``db.delete(file)``. A note left behind names an id the next file may
    be given. Called in the purging transaction; does not commit.
    """
    ids = sorted({int(i) for i in library_file_ids if i is not None})
    if ids:
        await db.execute(delete(LibraryFileNote).where(LibraryFileNote.library_file_id.in_(ids)))


async def _cancel_pending_queue_items(db: AsyncSession, library_file_id: int) -> None:
    """Cancel the pending jobs that cannot print once this file is gone.

    A queue row that outlives its file cannot dispatch, and until #2819 it was
    left to find that out at the printer — days later, as "Library file not
    found" — or, on the external branch below, was deleted outright with no
    error and no history.

    ⚠️ **Since m173 that is only half the rows.** A job that already holds its own
    copy of the bytes prints perfectly well without this file, so cancelling it
    would destroy work for no reason (spec §10). Which rows those are, on both
    tiers, is ``queue_source_release``'s question — it is the collector's owner
    question read from the other side, and it is asked in exactly one place. The
    queues it cancelled rows on wait, on the session, for the caller's commit:
    the caller then calls ``queue_source_release.recount_released_queues``.

    ⚠️ **Only ``pending`` rows.** ``printing`` is a race the printer-side fail
    path catches anyway, and completed / failed / cancelled rows are history:
    rewriting those would edit the record of what actually happened.

    Twin of ``archive_purge.ArchivePurgeService._cancel_pending_queue_items``,
    which does the same for a trashed archive. Both leave the row visible with
    a reason rather than removing it, because a job that vanished silently is
    indistinguishable from one that was never queued.

    Does not commit; the caller owns the transaction.
    """
    await queue_source_release.source_trashed(
        db, library_file_ids=[library_file_id], reason=queue_source_release.REASON_FILE_DELETED
    )


class LibraryTrashService:
    """Manages the trash retention sweeper and admin-triggered bulk purges."""

    def __init__(self):
        self._scheduler_task: asyncio.Task | None = None
        # Tick every 15 minutes — the window is a day, so this is plenty
        # responsive without burning CPU.
        self._check_interval = 900
        # Most-recent auto-purge tick result (in-memory, restart-volatile).
        # Surfaced through ``/library/trash/settings`` so the Settings UI can
        # show "last run / next run" cards alongside the toggle.
        self._last_result: LibraryPurgeRunResult | None = None

    async def start_scheduler(self):
        """Start the background sweeper task (idempotent)."""
        if self._scheduler_task is not None:
            return
        logger.info("Starting library trash sweeper")
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def stop_scheduler(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            logger.info("Stopped library trash sweeper")

    async def _scheduler_loop(self):
        while True:
            try:
                await asyncio.sleep(self._check_interval)
                async with async_session() as db:
                    await self._sweep(db)
                    await self._maybe_run_auto_purge(db)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Error in library trash sweeper: %s", e)
                await asyncio.sleep(60)

    # ---- Settings -----------------------------------------------------

    async def get_retention_days(self, db: AsyncSession | None = None) -> int:
        if db is None:
            async with async_session() as session:
                return await self._read_retention(session)
        return await self._read_retention(db)

    @staticmethod
    async def _read_retention(db: AsyncSession) -> int:
        result = await db.execute(select(Settings.value).where(Settings.key == TRASH_RETENTION_KEY))
        raw = result.scalar_one_or_none()
        if raw is None:
            return DEFAULT_RETENTION_DAYS
        try:
            days = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_RETENTION_DAYS
        return max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, days))

    async def set_retention_days(self, db: AsyncSession, days: int) -> int:
        """Persist the retention window. Clamped to [MIN, MAX]."""
        clamped = max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, int(days)))
        result = await db.execute(select(Settings).where(Settings.key == TRASH_RETENTION_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(Settings(key=TRASH_RETENTION_KEY, value=str(clamped)))
        else:
            row.value = str(clamped)
        await db.commit()
        return clamped

    @staticmethod
    async def _read_setting(db: AsyncSession, key: str) -> str | None:
        result = await db.execute(select(Settings.value).where(Settings.key == key))
        return result.scalar_one_or_none()

    @staticmethod
    async def _write_setting(db: AsyncSession, key: str, value: str) -> None:
        result = await db.execute(select(Settings).where(Settings.key == key))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(Settings(key=key, value=value))
        else:
            row.value = value

    async def get_auto_purge_settings(self, db: AsyncSession) -> dict:
        """Return the current auto-purge config.

        Returns a dict with ``enabled`` (bool), ``days`` (int, clamped) and
        ``include_never_printed`` (bool). Missing keys default to disabled /
        90 days / include-never-printed-on, matching the manual purge UX.
        """
        enabled_raw = await self._read_setting(db, AUTO_PURGE_ENABLED_KEY)
        days_raw = await self._read_setting(db, AUTO_PURGE_DAYS_KEY)
        incl_raw = await self._read_setting(db, AUTO_PURGE_INCLUDE_NEVER_PRINTED_KEY)

        enabled = (enabled_raw or "false").lower() == "true"
        try:
            days = int(days_raw) if days_raw is not None else DEFAULT_AUTO_PURGE_DAYS
        except (TypeError, ValueError):
            days = DEFAULT_AUTO_PURGE_DAYS
        days = max(MIN_AUTO_PURGE_DAYS, min(MAX_AUTO_PURGE_DAYS, days))
        include_never_printed = (incl_raw or "true").lower() == "true"
        return {
            "enabled": enabled,
            "days": days,
            "include_never_printed": include_never_printed,
        }

    async def set_auto_purge_settings(
        self,
        db: AsyncSession,
        *,
        enabled: bool,
        days: int,
        include_never_printed: bool,
    ) -> dict:
        """Persist auto-purge config; returns the saved (clamped) values."""
        clamped_days = max(MIN_AUTO_PURGE_DAYS, min(MAX_AUTO_PURGE_DAYS, int(days)))
        await self._write_setting(db, AUTO_PURGE_ENABLED_KEY, "true" if enabled else "false")
        await self._write_setting(db, AUTO_PURGE_DAYS_KEY, str(clamped_days))
        await self._write_setting(
            db,
            AUTO_PURGE_INCLUDE_NEVER_PRINTED_KEY,
            "true" if include_never_printed else "false",
        )
        await db.commit()
        return {
            "enabled": enabled,
            "days": clamped_days,
            "include_never_printed": include_never_printed,
        }

    async def _get_last_auto_purge_run(self, db: AsyncSession) -> datetime | None:
        raw = await self._read_setting(db, AUTO_PURGE_LAST_RUN_KEY)
        if not raw:
            return None
        try:
            # Stored as ISO 8601 UTC; tolerate both with and without 'Z' suffix.
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _stamp_last_auto_purge_run(self, db: AsyncSession, when: datetime, moved: int) -> None:
        await self._write_setting(db, AUTO_PURGE_LAST_RUN_KEY, when.isoformat())
        await self._write_setting(db, AUTO_PURGE_LAST_MOVED_KEY, str(int(moved)))
        await db.commit()

    async def _read_last_moved(self, db: AsyncSession) -> int | None:
        raw = await self._read_setting(db, AUTO_PURGE_LAST_MOVED_KEY)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def _maybe_run_auto_purge(self, db: AsyncSession) -> int:
        """If auto-purge is enabled and >=24h has elapsed since the last run, run it.

        Returns the number of files moved to trash (0 if disabled or throttled).
        The 24h throttle means a 15-minute sweeper cadence still only triggers
        one actual purge per day, keeping the DB churn predictable.

        ``purge_older_than`` itself stamps ``last_run`` + records the
        ``LibraryPurgeRunResult`` — so manual purges through the admin
        endpoint also push the 24h cycle forward.
        """
        cfg = await self.get_auto_purge_settings(db)
        if not cfg["enabled"]:
            return 0

        now = datetime.now(timezone.utc)
        last = await self._get_last_auto_purge_run(db)
        if last is not None and (now - last) < timedelta(hours=24):
            return 0

        moved = await self.purge_older_than(
            db,
            older_than_days=cfg["days"],
            include_never_printed=cfg["include_never_printed"],
        )
        if moved:
            logger.info("Library auto-purge: moved %d file(s) to trash (threshold=%d days)", moved, cfg["days"])
        return moved

    @property
    def last_result(self) -> LibraryPurgeRunResult | None:
        """Most-recent in-memory auto-purge tick result. None before first run."""
        return self._last_result

    async def get_status(self, db: AsyncSession) -> dict[str, Any]:
        """Return ``{enabled, days, include_never_printed, last_run, next_run_at}``.

        ``last_run`` mirrors the in-memory ``LibraryPurgeRunResult`` (restart-
        volatile — comes back as None after a process restart even if the
        persistent ``library_auto_purge_last_run`` setting still has the
        timestamp). When the in-memory result is gone but the persistent
        timestamp exists, ``last_run.finished_at`` is set to that timestamp
        and ``moved`` is reported as -1 to signal "we know it ran but the
        count was lost on restart" — UI shows the time without a count.

        ``next_run_at`` is the earliest future moment the auto-purge can
        fire: ``last_persisted + 24h`` (clamped to now if already past).
        When auto-purge has never run, it's the next scheduler tick (~15
        min from now). NULL when auto-mode is disabled.
        """
        cfg = await self.get_auto_purge_settings(db)
        last_persisted = await self._get_last_auto_purge_run(db)
        last_moved_persisted = await self._read_last_moved(db)

        last_run: dict[str, Any] | None = None
        if self._last_result is not None:
            last_run = self._last_result.as_dict()
        elif last_persisted is not None:
            # Server restarted between the run and now. Pull the count from
            # the persisted setting if it's there (writes by ``purge_older_than``
            # since the introduction of AUTO_PURGE_LAST_MOVED_KEY); fall back
            # to the -1 sentinel only for legacy stamps that pre-date that key.
            last_run = {
                "started_at": last_persisted.isoformat(),
                "finished_at": last_persisted.isoformat(),
                "moved": last_moved_persisted if last_moved_persisted is not None else -1,
            }

        next_run_at: datetime | None = None
        if cfg["enabled"]:
            now = datetime.now(timezone.utc)
            if last_persisted is None:
                # Never run yet — next tick is at most one ``_check_interval``
                # away. Add a small fudge so the UI shows "in N minutes"
                # rather than always "in 15 min" exactly.
                next_run_at = now + timedelta(seconds=self._check_interval)
            else:
                candidate = last_persisted + timedelta(hours=24)
                next_run_at = candidate if candidate > now else now

        return {
            "enabled": cfg["enabled"],
            "days": cfg["days"],
            "include_never_printed": cfg["include_never_printed"],
            "last_run": last_run,
            "next_run_at": next_run_at.isoformat() if next_run_at else None,
        }

    # ---- Preview / purge ---------------------------------------------

    async def preview_purge(
        self,
        db: AsyncSession,
        older_than_days: int,
        include_never_printed: bool = True,
        sample_limit: int = 5,
    ) -> dict:
        """Count + size of files eligible for purge. Reads only; never mutates."""
        if older_than_days < 1:
            return {"count": 0, "total_bytes": 0, "sample_filenames": []}
        now = datetime.now(timezone.utc)
        cutoff = _age_cutoff(now, older_than_days)
        clause = _purge_filter(cutoff, include_never_printed)

        count_result = await db.execute(select(func.count(LibraryFile.id)).where(clause))
        count = int(count_result.scalar() or 0)

        size_result = await db.execute(select(func.coalesce(func.sum(LibraryFile.file_size), 0)).where(clause))
        total_bytes = int(size_result.scalar() or 0)

        sample_result = await db.execute(
            select(LibraryFile.filename).where(clause).order_by(LibraryFile.created_at).limit(sample_limit)
        )
        samples = [row[0] for row in sample_result.all()]

        return {
            "count": count,
            "total_bytes": total_bytes,
            "sample_filenames": samples,
            "older_than_days": older_than_days,
            "include_never_printed": include_never_printed,
        }

    async def purge_older_than(
        self,
        db: AsyncSession,
        older_than_days: int,
        include_never_printed: bool = True,
    ) -> int:
        """Move matching files to trash (stamps ``deleted_at``). Returns count.

        Stamps ``library_auto_purge_last_run`` + records the in-memory
        ``LibraryPurgeRunResult`` regardless of caller (auto-tick or admin
        manual purge). That way a manual run through the admin UI also
        resets the 24h auto-cycle — admins shouldn't get an unexpected
        second purge tick a few minutes after they cleaned up by hand.
        """
        if older_than_days < 1:
            return 0
        started = datetime.now(timezone.utc)
        cutoff = _age_cutoff(started, older_than_days)
        clause = _purge_filter(cutoff, include_never_printed)

        # We need the IDs so callers can audit or display them if they want.
        # Doing a single UPDATE ... WHERE is safe even under concurrent
        # uploads — the clause already excludes rows with deleted_at set.
        id_result = await db.execute(select(LibraryFile.id).where(clause))
        ids = [row[0] for row in id_result.all()]
        moved = len(ids)
        if moved:
            await db.execute(LibraryFile.__table__.update().where(LibraryFile.id.in_(ids)).values(deleted_at=started))
            await part_images.mark_files_changed(db, ids)
            await db.commit()
            logger.info("Library purge: moved %d file(s) to trash (older_than_days=%d)", moved, older_than_days)

        # Stamp + record the result on every successful call (even when
        # nothing was moved — "ran it, found nothing" is still a run).
        result = LibraryPurgeRunResult(started_at=started, moved=moved)
        result.finished_at = datetime.now(timezone.utc)
        self._last_result = result
        await self._stamp_last_auto_purge_run(db, started, moved)
        return moved

    # ---- Sweeper ------------------------------------------------------

    @staticmethod
    async def _active_archive_refs(db: AsyncSession, file_id: int) -> int:
        """Count active (non-trashed) PrintArchive rows that reference this file.

        Used by hard-delete paths so a library file isn't unlinked from disk
        while live archives still need it for reprint / chain-of-custody.
        Trashed archives don't count — the user has signalled they're OK with
        the chain breaking once the archive trash retention also elapses.
        """
        result = await db.execute(
            select(func.count(PrintArchive.id)).where(
                PrintArchive.library_file_id == file_id,
                PrintArchive.deleted_at.is_(None),
            )
        )
        return int(result.scalar() or 0)

    async def _purge_retention_batch(self, db, batch):
        from backend.app.services.library_delete_cleanup import finish
        from backend.app.services.library_delete_scope import library_delete_scope

        async with library_delete_scope(db, batch):
            for proof in batch:
                current = await db.get(LibraryFile, proof.id, populate_existing=True)
                await self.purge_rows(db, current)
            await db.commit()
        await finish(db)
        # After the commit and outside the plan: the queues the purge cancelled rows on.
        await queue_source_release.recount_released_queues(db)

    async def _sweep(self, db: AsyncSession) -> int:
        """Hard-delete trashed rows whose retention window has elapsed.

        Active archive back-references are detached (not respected as a
        pin) — the archive carries its own ``file_path`` to disk-stored
        gcode + thumbnails (chain-of-custody invariant from m009/m039),
        so blanking ``library_file_id`` is safe. Same contract as the
        user-triggered hard-delete + empty-trash paths.
        """
        retention = await self._read_retention(db)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention)

        result = await db.execute(
            select(LibraryFile).where(
                LibraryFile.deleted_at.isnot(None),
                LibraryFile.deleted_at < cutoff,
            )
        )
        rows = result.scalars().all()
        if not rows:
            return 0

        from backend.app.services.library_identity import LibraryIdentity

        proofs = [LibraryIdentity.capture(row) for row in rows]
        await db.commit()
        deleted, failures = 0, 0

        for start in range(0, len(proofs), 100):
            batch = proofs[start : start + 100]
            try:
                await self._purge_retention_batch(db, batch)
                deleted += len(batch)
            except Exception:
                await db.rollback()
                if len(batch) == 1:
                    failures += 1
                    continue
                # The refused batch rolled back as a whole. A busy/retained
                # neighbour must not pin every other expired file indefinitely.
                for proof in batch:
                    try:
                        await self._purge_retention_batch(db, [proof])
                        deleted += 1
                    except Exception:
                        await db.rollback()
                        failures += 1
        if failures:
            logger.warning("Library trash sweep retained %d row(s)", failures)
        logger.info("Library trash sweeper: hard-deleted %d row(s) past %d-day retention", deleted, retention)
        return deleted

    # ---- User-facing trash ops ----------------------------------------
    async def trash_or_purge(self, db: AsyncSession, file: LibraryFile, *, detach_folder: bool = False) -> bool:
        """What deleting one library file MEANS. Returns True if it was trashed.

        The single delete route, the bulk route and both folder routes all
        answer this question, and for a long time they answered it differently:
        one soft-deleted while three unlinked the bytes and dropped the row, so
        a multi-select delete was permanent while the identical single delete
        was reversible. There is one answer now, and it lives here.

        A managed file is soft-deleted: bytes, thumbnail and queue references
        stay, and the sweeper takes them after the retention window. An
        EXTERNAL file is purged instead — its bytes are outside BamDude and
        there is nothing to restore, which is what the single route has always
        done.

        ``detach_folder`` is for deleting a folder. ``library_files.folder_id``
        is ``ON DELETE CASCADE``, so a trashed row inside a folder that is then
        removed would be deleted anyway — the trip to the trash would be
        theatre. Detaching first means the file survives, and restore puts it
        at the library root, which is the only honest destination once its
        folder is gone. Does not commit; the caller owns the transaction, and
        after its commit (outside its lock plan) calls
        ``queue_source_release.recount_released_queues``.
        """
        from backend.app.services.library_delete_scope import require_library_plan

        require_library_plan(db, file)
        if file.is_external:
            await self.purge_rows(db, file)
            return False

        await _cancel_pending_queue_items(db, file.id)
        file.deleted_at = datetime.now(timezone.utc)
        await part_images.mark_files_changed(db, [file.id])  # a trashed file is no source (spec part-thumbnails §12.3)
        if detach_folder:
            file.folder_id = None
        return True

    async def restore(self, db: AsyncSession, file: LibraryFile) -> LibraryFile:
        """Clear ``deleted_at`` so the file reappears in listings."""
        file.deleted_at = None
        await part_images.mark_files_changed(db, [file.id])  # its plates show pictures again
        await db.commit()
        await db.refresh(file)
        return file

    async def purge_rows(self, db: AsyncSession, file: LibraryFile, *, reason=queue_source_release.REASON_FILE_DELETED):
        """Flush-only shared hard-delete phase under the caller's whole plan.

        After its commit the caller runs ``library_delete_cleanup.finish`` and
        ``queue_source_release.recount_released_queues``, both outside the plan.
        """
        from backend.app.services.content_delete_rows import library_rows
        from backend.app.services.library_delete_scope import require_library_plan

        require_library_plan(db, file)
        from fastapi import HTTPException

        from backend.app.models.auto_queue import AutoQueueItem
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.queue_source import QueueSource

        for model in (PrintQueueItem, AutoQueueItem):
            active = await db.execute(
                select(model.id, QueueSource.state)
                .outerjoin(QueueSource, QueueSource.id == model.queue_source_id)
                .where(
                    model.library_file_id == file.id,
                    model.status.in_(("printing", "paused", "dispatching", "assigned")),
                )
            )
            if any(state != "ready" for _row_id, state in active):
                raise HTTPException(
                    409,
                    {
                        "error": "file_in_use",
                        "message": "An active print still needs this original file. Wait for it to finish, then retry.",
                    },
                )
        return await library_rows(db, file, reason=reason)

    async def hard_delete_now(self, db: AsyncSession, file: LibraryFile) -> None:
        """Standalone hard delete; compound roots use purge_rows without a nested commit."""
        from backend.app.services.library_delete_cleanup import finish
        from backend.app.services.library_delete_scope import library_delete_scope
        from backend.app.services.library_identity import LibraryIdentity

        identity = LibraryIdentity.capture(file)
        async with library_delete_scope(db, [identity]):
            current = await db.get(LibraryFile, identity.id, populate_existing=True)
            await self.purge_rows(db, current)
            await db.commit()
        await finish(db)
        await queue_source_release.recount_released_queues(db)

    async def active_archive_references(self, db: AsyncSession, file_id: int) -> int:
        """Public wrapper around the internal counter — for routes / API."""
        return await self._active_archive_refs(db, file_id)


library_trash_service = LibraryTrashService()
