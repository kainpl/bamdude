"""Archive trash service.

Two-stage archive deletion mirroring the library trash (#1008):

1. Users / admins soft-delete archives — the row stays in ``print_archives``
   with ``deleted_at`` stamped; the on-disk bytes (3MF, thumbnail, timelapse,
   photos) remain in place so a restore is a metadata-only operation.

2. A background sweeper hard-deletes rows whose ``deleted_at`` is older than
   the configured archive-trash retention window. Hard-delete goes through
   :meth:`ArchiveService.delete_archive` so on-disk cleanup is the same as
   manual hard-delete.

The earlier per-row "auto-purge by activity age" feature was removed in 0.4.2
— it overlapped with ``archive_cleanup_service`` (which prunes 3MF bytes
per design chain rather than per row) and actively destroyed history of
frequently-reprinted designs (each reprint creates a new archive row, so
old chain siblings would individually fall past the threshold even when
the design was still hot). ``archive_cleanup_service`` is the recommended
disk-reclaim path; manual delete + this trash sweeper handle the explicit
"delete this archive" workflow with a recovery window.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database as _database
from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.services.archive import ArchiveService

logger = logging.getLogger(__name__)

TRASH_RETENTION_KEY = "archive_trash_retention_days"

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365


class ArchivePurgeService:
    """Manages the archive-trash retention sweeper + per-row trash operations.

    Soft-delete via :meth:`move_to_trash`; hard-delete via the sweeper /
    :meth:`hard_delete_now` / :meth:`empty_trash`.
    """

    def __init__(self):
        self._scheduler_task: asyncio.Task | None = None
        self._check_interval = 900

    async def start_scheduler(self):
        if self._scheduler_task is not None:
            return
        logger.info("Starting archive trash sweeper")
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
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Error in archive trash sweeper: %s", e)
                await asyncio.sleep(60)

    # ---- Settings -----------------------------------------------------

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

    # ---- Trash operations ---------------------------------------------

    @staticmethod
    async def move_to_trash(db: AsyncSession, archive: PrintArchive) -> PrintArchive:
        """Stamp ``deleted_at`` on a single archive (manual delete path)."""
        archive.deleted_at = datetime.now(timezone.utc)
        # A pending queue item whose source archive was just trashed can never
        # dispatch — its 3MF is gone from disk. Cancel it with a clear reason
        # instead of leaving it stuck 'pending' forever (#1348 follow-up).
        #
        # ⚠️ The FK removes nothing. It used to read ON DELETE CASCADE, which at
        # hard-delete time deleted the queue rows on PostgreSQL — and on SQLite
        # never did anything at all, because ``PRAGMA foreign_keys`` is off in
        # this codebase, so the comment that claimed it was "already handled"
        # was false on the backend most installs run. m173 made the rule SET
        # NULL: a job that owns a local copy of its bytes has to survive its
        # archive going away. The cancel below is therefore the whole of what
        # happens to a pending row, on both dialects — the link is NOT cut here,
        # because a trashed archive can be restored; the code-level detach SQLite
        # needs runs at hard-delete time, in ``ArchiveService.delete_archive``.
        await ArchivePurgeService._cancel_pending_queue_items(db, archive.id)
        await db.commit()
        await db.refresh(archive)
        return archive

    @staticmethod
    async def _cancel_pending_queue_items(db: AsyncSession, archive_id: int) -> None:
        """Cancel the pending jobs that cannot print once this archive is gone.

        Only ``pending`` rows are touched — ``printing`` is a rare race the
        printer-side fail path catches anyway, and completed / failed /
        cancelled rows are historical. Does not commit; the caller's
        transaction does.

        ⚠️ **Two things this did not do, both answered in one place now (§10).** It
        reached ``print_queue`` only, so a pending auto-queue row naming the same
        archive was told nothing at all and merely stopped being routable; and it
        cancelled every pending row, including the ones that hold their own copy of
        the bytes and print perfectly well without this archive.

        ⚠️ Not recounted here: :meth:`move_to_trash` commits inside the trash
        route's ``archive_write_scope``, and a recount is a write that must not run
        inside a deletion's lock plan. The recount
        (``queue_source_release.recount_released_queues``) belongs to the route,
        once that scope has ended.
        """
        from backend.app.services import queue_source_release

        await queue_source_release.source_trashed(
            db, archive_ids=[archive_id], reason=queue_source_release.REASON_ARCHIVE_DELETED
        )

    @staticmethod
    async def count_related_queue_items(db: AsyncSession, archive_id: int) -> tuple[int, int]:
        """Count queue items pointing at *archive_id* (#1734 delete pre-flight).

        Returns ``(total, in_flight)`` where ``in_flight`` is the number of
        related rows that are currently ``printing`` or ``paused`` — i.e.
        mid-print, which blocks the delete (the running print needs its
        backing archive for the metadata trail). A multi-plate "Send All"
        (#1733) backs one archive with N per-plate rows, so the total lets
        the delete-confirm modal warn how many queue lines will disappear.
        Terminal rows (completed / failed / cancelled) are counted in the
        total but never block.
        """
        from sqlalchemy import func as sa_func

        from backend.app.models.print_queue import PrintQueueItem

        total = (
            await db.scalar(
                select(sa_func.count()).select_from(PrintQueueItem).where(PrintQueueItem.archive_id == archive_id)
            )
        ) or 0
        in_flight = (
            await db.scalar(
                select(sa_func.count())
                .select_from(PrintQueueItem)
                .where(
                    PrintQueueItem.archive_id == archive_id,
                    PrintQueueItem.status.in_(("printing", "paused")),
                )
            )
        ) or 0
        return int(total), int(in_flight)

    @staticmethod
    async def restore(db: AsyncSession, archive: PrintArchive) -> PrintArchive:
        """Clear ``deleted_at`` so the archive reappears in listings."""
        archive.deleted_at = None
        await db.commit()
        await db.refresh(archive)
        return archive

    @staticmethod
    async def hard_delete_now(archive_id: int, *, retention_cutoff: datetime | None = None) -> bool:
        """Hard-delete an already-trashed archive bypassing the retention window.

        Runs in its own session via ``ArchiveService.delete_archive`` so the
        on-disk cleanup (3MF, thumbnail, timelapse, source 3MF, F3D, photos)
        goes through the same safety-checked path as the sweeper. The service
        rechecks that it is still in trash under the archive's row lock: an
        operator restore that committed after the caller's scan must survive.

        ``delete_archive`` commits and leaves its archive guard before it returns,
        so the printer queues its release cancelled rows on are recounted here —
        after the commit, outside the plan, in a transaction of their own.
        """
        from backend.app.services import queue_source_release

        async with _database.async_session() as delete_db:
            service = ArchiveService(delete_db)
            deleted = await service.delete_archive(
                archive_id, refuse_kept=True, trashed_only=True, retention_cutoff=retention_cutoff
            )
            await queue_source_release.recount_released_queues(delete_db)
            return deleted

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
        failed = []
        # The scan reader must not retain SQLite's transaction while another
        # owning session takes its writer.
        await db.commit()
        for archive_id in ids:
            try:
                if await self.hard_delete_now(archive_id, retention_cutoff=cutoff):
                    deleted += 1
                else:
                    failed.append(archive_id)
            except Exception:
                failed.append(archive_id)
        if failed:
            logger.warning("Archive trash sweep retained %d row(s); sample=%s", len(failed), failed[:10])
        logger.info("Archive trash sweeper: hard-deleted %d archive(s) past %d-day retention", deleted, retention)
        return deleted


archive_purge_service = ArchivePurgeService()
