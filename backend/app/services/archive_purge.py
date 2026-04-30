"""Archive trash + auto-purge service (#1008 follow-up).

Two-stage archive deletion mirroring the library trash (#1008):

1. Users / admins soft-delete archives — the row stays in ``print_archives``
   with ``deleted_at`` stamped; the on-disk bytes (3MF, thumbnail, timelapse,
   photos) remain in place so a restore is a metadata-only operation.

2. A background sweeper hard-deletes rows whose ``deleted_at`` is older than
   the configured archive-trash retention window. Hard-delete goes through
   :meth:`ArchiveService.delete_archive` so on-disk cleanup is the same as
   manual hard-delete.

Auto-purge (admin-configured age threshold) and manual purge both stamp
``deleted_at`` rather than calling ``delete_archive`` directly, so the user
has a restore window. Hard-deletion is opt-in via "Empty trash" / per-row
"Delete now" buttons or happens automatically once retention elapses.

Why a separate retention setting from library trash: archives carry print
history that's expensive to recreate (no slicer round-trip) and may have
operational value (failure investigation, energy/cost tracking). Operators
running a tight ship may want a longer archive retention than library trash
even when both bins have the same purpose.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database as _database
from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.services.archive import ArchiveService

logger = logging.getLogger(__name__)

AUTO_PURGE_ENABLED_KEY = "archive_auto_purge_enabled"
AUTO_PURGE_DAYS_KEY = "archive_auto_purge_days"
AUTO_PURGE_LAST_RUN_KEY = "archive_auto_purge_last_run"
TRASH_RETENTION_KEY = "archive_trash_retention_days"

DEFAULT_AUTO_PURGE_DAYS = 365
# 7-day floor mirrors the library auto-purge; anything shorter treats archives
# as ephemeral which is rarely what anyone wants.
MIN_AUTO_PURGE_DAYS = 7
MAX_AUTO_PURGE_DAYS = 3650

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365


def _age_cutoff(now: datetime, older_than_days: int) -> datetime:
    return now - timedelta(days=older_than_days)


def _last_activity_expr():
    """Most-recent timestamp on an archive row.

    Reprints reuse the archive row and update ``completed_at``/``started_at`` but
    leave ``created_at`` pinned to the first print, so purging on ``created_at``
    would evict recently-reprinted archives. Use the latest of the three instead.
    """
    return func.coalesce(
        PrintArchive.completed_at,
        PrintArchive.started_at,
        PrintArchive.created_at,
    )


class ArchivePurgeService:
    """Manages archive trash retention sweeper + admin-triggered manual purge.

    Despite the name (kept for backward compat with the existing route /
    settings keys), this service now implements the full two-stage trash flow:
    soft-delete via ``purge_older_than`` / ``move_to_trash``, hard-delete via
    the sweeper / ``hard_delete_now`` / ``empty_trash``.
    """

    def __init__(self):
        self._scheduler_task: asyncio.Task | None = None
        # Match library trash cadence — the 24h throttle keeps actual work rare.
        self._check_interval = 900

    async def start_scheduler(self):
        if self._scheduler_task is not None:
            return
        logger.info("Starting archive trash sweeper + auto-purge")
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def stop_scheduler(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            logger.info("Stopped archive trash sweeper")

    async def _scheduler_loop(self):
        while True:
            try:
                await asyncio.sleep(self._check_interval)
                async with _database.async_session() as db:
                    await self._sweep(db)
                    await self._maybe_run_auto_purge(db)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Error in archive trash sweeper: %s", e)
                await asyncio.sleep(60)

    # ---- Settings -----------------------------------------------------

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

    async def get_settings(self, db: AsyncSession) -> dict:
        """Return ``{enabled, days}``. Missing keys default to disabled / 365d."""
        enabled_raw = await self._read_setting(db, AUTO_PURGE_ENABLED_KEY)
        days_raw = await self._read_setting(db, AUTO_PURGE_DAYS_KEY)

        enabled = (enabled_raw or "false").lower() == "true"
        try:
            days = int(days_raw) if days_raw is not None else DEFAULT_AUTO_PURGE_DAYS
        except (TypeError, ValueError):
            days = DEFAULT_AUTO_PURGE_DAYS
        days = max(MIN_AUTO_PURGE_DAYS, min(MAX_AUTO_PURGE_DAYS, days))
        return {"enabled": enabled, "days": days}

    async def set_settings(self, db: AsyncSession, *, enabled: bool, days: int) -> dict:
        clamped_days = max(MIN_AUTO_PURGE_DAYS, min(MAX_AUTO_PURGE_DAYS, int(days)))
        await self._write_setting(db, AUTO_PURGE_ENABLED_KEY, "true" if enabled else "false")
        await self._write_setting(db, AUTO_PURGE_DAYS_KEY, str(clamped_days))
        await db.commit()
        return {"enabled": enabled, "days": clamped_days}

    async def get_retention_days(self, db: AsyncSession | None = None) -> int:
        if db is None:
            async with _database.async_session() as session:
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
        clamped = max(MIN_RETENTION_DAYS, min(MAX_RETENTION_DAYS, int(days)))
        result = await db.execute(select(Settings).where(Settings.key == TRASH_RETENTION_KEY))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(Settings(key=TRASH_RETENTION_KEY, value=str(clamped)))
        else:
            row.value = str(clamped)
        await db.commit()
        return clamped

    async def _get_last_run(self, db: AsyncSession) -> datetime | None:
        raw = await self._read_setting(db, AUTO_PURGE_LAST_RUN_KEY)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _stamp_last_run(self, db: AsyncSession, when: datetime) -> None:
        await self._write_setting(db, AUTO_PURGE_LAST_RUN_KEY, when.isoformat())
        await db.commit()

    async def _maybe_run_auto_purge(self, db: AsyncSession) -> int:
        """Run the auto-purge if enabled and >=24h has elapsed since last run."""
        cfg = await self.get_settings(db)
        if not cfg["enabled"]:
            return 0

        now = datetime.now(timezone.utc)
        last = await self._get_last_run(db)
        if last is not None and (now - last) < timedelta(hours=24):
            return 0

        moved = await self.purge_older_than(db, older_than_days=cfg["days"])
        await self._stamp_last_run(db, now)
        if moved:
            logger.info(
                "Archive auto-purge: moved %d archive(s) to trash (threshold=%d days)",
                moved,
                cfg["days"],
            )
        return moved

    # ---- Preview / purge ---------------------------------------------

    async def preview_purge(
        self,
        db: AsyncSession,
        older_than_days: int,
        sample_limit: int = 5,
    ) -> dict:
        """Count + size of archives eligible for purge. Read-only."""
        if older_than_days < 1:
            return {
                "count": 0,
                "total_bytes": 0,
                "sample_filenames": [],
                "older_than_days": older_than_days,
            }
        now = datetime.now(timezone.utc)
        cutoff = _age_cutoff(now, older_than_days)
        last_activity = _last_activity_expr()
        # Only count active (non-trashed) archives — trashed ones are already
        # counted against the trash bin's own retention sweeper.
        clause = (last_activity < cutoff) & PrintArchive.deleted_at.is_(None)

        count_result = await db.execute(select(func.count(PrintArchive.id)).where(clause))
        count = int(count_result.scalar() or 0)

        size_result = await db.execute(select(func.coalesce(func.sum(PrintArchive.file_size), 0)).where(clause))
        total_bytes = int(size_result.scalar() or 0)

        sample_result = await db.execute(
            select(PrintArchive.filename).where(clause).order_by(last_activity).limit(sample_limit)
        )
        samples = [row[0] for row in sample_result.all()]

        return {
            "count": count,
            "total_bytes": total_bytes,
            "sample_filenames": samples,
            "older_than_days": older_than_days,
        }

    async def purge_older_than(self, db: AsyncSession, older_than_days: int) -> int:
        """Move archives older than ``older_than_days`` to the trash bin.

        Stamps ``deleted_at`` on matching rows so they disappear from listings
        but remain restorable until the retention sweeper hard-deletes them.
        Mirrors the library trash flow — operators have a window to recover an
        archive that was purged in error.
        """
        if older_than_days < 1:
            return 0
        now = datetime.now(timezone.utc)
        cutoff = _age_cutoff(now, older_than_days)
        clause = (_last_activity_expr() < cutoff) & PrintArchive.deleted_at.is_(None)

        id_result = await db.execute(select(PrintArchive.id).where(clause))
        ids = [row[0] for row in id_result.all()]
        if not ids:
            return 0

        await db.execute(PrintArchive.__table__.update().where(PrintArchive.id.in_(ids)).values(deleted_at=now))
        await db.commit()
        logger.info(
            "Archive purge: moved %d archive(s) to trash (older_than_days=%d)",
            len(ids),
            older_than_days,
        )
        return len(ids)

    # ---- Trash operations ---------------------------------------------

    @staticmethod
    async def move_to_trash(db: AsyncSession, archive: PrintArchive) -> PrintArchive:
        """Stamp ``deleted_at`` on a single archive (manual delete path)."""
        archive.deleted_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(archive)
        return archive

    @staticmethod
    async def restore(db: AsyncSession, archive: PrintArchive) -> PrintArchive:
        """Clear ``deleted_at`` so the archive reappears in listings."""
        archive.deleted_at = None
        await db.commit()
        await db.refresh(archive)
        return archive

    @staticmethod
    async def hard_delete_now(archive_id: int) -> bool:
        """Hard-delete an already-trashed archive bypassing the retention window.

        Runs in its own session via ``ArchiveService.delete_archive`` so the
        on-disk cleanup (3MF, thumbnail, timelapse, source 3MF, F3D, photos)
        goes through the same safety-checked path as the sweeper. Caller
        should verify the archive is in trash before invoking.
        """
        async with _database.async_session() as delete_db:
            service = ArchiveService(delete_db)
            return await service.delete_archive(archive_id)

    async def empty_trash(self, db: AsyncSession) -> int:
        """Hard-delete every trashed archive immediately. Returns the count."""
        id_result = await db.execute(select(PrintArchive.id).where(PrintArchive.deleted_at.isnot(None)))
        ids = [row[0] for row in id_result.all()]
        if not ids:
            return 0
        deleted = 0
        for archive_id in ids:
            if await self.hard_delete_now(archive_id):
                deleted += 1
        if deleted:
            logger.info("Archive trash emptied: hard-deleted %d archive(s)", deleted)
        return deleted

    # ---- Sweeper ------------------------------------------------------

    async def _sweep(self, db: AsyncSession) -> int:
        """Hard-delete trashed archive rows whose retention window has elapsed."""
        retention = await self._read_retention(db)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention)

        result = await db.execute(
            select(PrintArchive.id).where(
                PrintArchive.deleted_at.isnot(None),
                PrintArchive.deleted_at < cutoff,
            )
        )
        ids = [row[0] for row in result.all()]
        if not ids:
            return 0

        deleted = 0
        for archive_id in ids:
            if await self.hard_delete_now(archive_id):
                deleted += 1
        # Defensive sweep — if delete_archive somehow left rows behind, drop
        # the orphaned ``print_archives`` row so the sweeper doesn't get stuck
        # re-processing it forever.
        await db.execute(delete(PrintArchive).where(PrintArchive.id.in_(ids), PrintArchive.deleted_at.isnot(None)))
        await db.commit()
        logger.info("Archive trash sweeper: hard-deleted %d archive(s) past %d-day retention", deleted, retention)
        return deleted


archive_purge_service = ArchivePurgeService()
