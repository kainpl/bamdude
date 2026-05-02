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
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings
from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.settings import Settings

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


def _to_absolute_path(relative_path: str | None) -> Path | None:
    """Mirror of the routes helper so this service has no route-module import.

    Accepts the legacy absolute paths that predate the relative-path migration
    verbatim; new rows always store paths relative to ``base_dir``.
    """
    if not relative_path:
        return None
    path = Path(relative_path)
    if path.is_absolute():
        return path
    return Path(app_settings.base_dir) / path


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

    async def _sweep(self, db: AsyncSession) -> int:
        """Hard-delete trashed rows whose retention window has elapsed.

        Skips rows still referenced by active (non-trashed) archives — those
        files are pinned until the referencing archives also trash out, since
        otherwise a reprint of an active archive would lose its source 3MF
        mid-flight.
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

        # Filter out rows still referenced by active archives. They wait —
        # either the user restores them, or the referencing archive itself
        # trashes out and the next sweep tick picks them up.
        eligible_rows: list[LibraryFile] = []
        pinned = 0
        for row in rows:
            refs = await self._active_archive_refs(db, row.id)
            if refs > 0:
                pinned += 1
                continue
            eligible_rows.append(row)
        if pinned:
            logger.info("Library trash sweeper: %d row(s) pinned by active archive references — waiting", pinned)
        if not eligible_rows:
            return 0

        deleted = 0
        for row in eligible_rows:
            self._unlink_on_disk(row)
            deleted += 1
        eligible_ids = [r.id for r in eligible_rows]
        # Detach archive rows BEFORE deleting library files. The FK column
        # ``print_archives.library_file_id`` is declared ``ON DELETE SET
        # NULL``, but SQLite ignores FK actions unless ``PRAGMA
        # foreign_keys = ON`` is set on every connection — which BamDude
        # deliberately doesn't (would require a separate audit of every
        # other FK in the schema). Without this explicit UPDATE the
        # archive rows would be left with stale ``library_file_id``
        # pointers to a now-gone library file. The two route-side delete
        # paths in ``api/routes/library.py`` already do the same thing —
        # this brings the sweeper + hard_delete_now in line with them.
        await db.execute(
            update(PrintArchive).where(PrintArchive.library_file_id.in_(eligible_ids)).values(library_file_id=None)
        )
        await db.execute(delete(LibraryFile).where(LibraryFile.id.in_(eligible_ids)))
        await db.commit()
        logger.info("Library trash sweeper: hard-deleted %d row(s) past %d-day retention", deleted, retention)
        return deleted

    @staticmethod
    def _unlink_on_disk(row: LibraryFile) -> None:
        """Best-effort cleanup of the file + thumbnail on disk."""
        for rel in (row.file_path, row.thumbnail_path):
            abs_path = _to_absolute_path(rel)
            if abs_path is None:
                continue
            try:
                if abs_path.exists():
                    abs_path.unlink()
            except OSError as e:
                logger.warning("Trash sweep: failed to unlink %s: %s", abs_path, e)

    # ---- User-facing trash ops ----------------------------------------

    async def restore(self, db: AsyncSession, file: LibraryFile) -> LibraryFile:
        """Clear ``deleted_at`` so the file reappears in listings."""
        file.deleted_at = None
        await db.commit()
        await db.refresh(file)
        return file

    async def hard_delete_now(self, db: AsyncSession, file: LibraryFile) -> None:
        """Bypass retention and delete this trashed file + its bytes immediately.

        Caller is expected to verify there are no active archive references
        first (via :meth:`active_archive_references` or the count helper) and
        return 409 to the user if there are. We don't raise here so the
        sweeper / empty-trash path can keep its existing skip-and-log shape.

        Trashed archives that still reference this row by hash get their
        ``library_file_id`` blanked out (not cascade-deleted) — see the
        comment in ``_sweep`` for why we do this in code rather than relying
        on the schema-level ``ON DELETE SET NULL``.
        """
        self._unlink_on_disk(file)
        await db.execute(
            update(PrintArchive).where(PrintArchive.library_file_id == file.id).values(library_file_id=None)
        )
        await db.delete(file)
        await db.commit()

    async def active_archive_references(self, db: AsyncSession, file_id: int) -> int:
        """Public wrapper around the internal counter — for routes / API."""
        return await self._active_archive_refs(db, file_id)


library_trash_service = LibraryTrashService()
