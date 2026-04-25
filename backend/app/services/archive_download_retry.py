"""On-demand 3MF download retry for fallback archives.

When ``on_print_start`` can't pull the 3MF file from the printer (FTP
hiccup, path mismatch, slow SD, transient disconnect) a fallback
archive is created with ``file_path=""``.  This service offers three
one-shot retry triggers — there is intentionally **no** background
periodic loop, because many prints are shorter than any reasonable
periodic cycle:

1. **Startup sweep** — on BamDude startup, try every ``status='printing'``
   fallback archive once (useful after a backend restart).
2. **On printer connect** — when a printer reconnects, try its
   ``status='printing'`` fallback archives once.
3. **Manual** — ``POST /archives/{id}/retry-download`` calls
   :meth:`retry_archive` to attempt a single download for that archive.

The final attempt before SD cleanup (``on_print_complete``) is inlined
in ``main.py`` directly (the last-chance block), since it needs to run
between archive-id resolution and SD-file deletion.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

from sqlalchemy import or_, select

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services.archive import ArchiveService
from backend.app.services.archive_download import try_download_3mf

logger = logging.getLogger(__name__)

# Result status of a retry attempt:
#   recovered         — 3MF downloaded + attached to the archive row
#   already_has_file  — archive already populated, nothing to do
#   in_progress       — another retry for this archive is currently running
#   failed            — download failed (FTP unreachable / file not on SD)
#   error             — unexpected error (archive / printer not found, etc.)
RetryStatus = Literal["recovered", "already_has_file", "in_progress", "failed", "error"]


class ArchiveDownloadRetryService:
    """On-demand 3MF download retries — no background loop."""

    def __init__(self):
        # Per-archive in-progress guard.  Lives across the lifetime of
        # one retry call (FTP download + attach).  Any concurrent caller
        # (startup sweep, connect hook, last-chance, manual API) sees
        # the lock taken and skips immediately instead of starting a
        # duplicate FTP session.
        self._in_progress: set[int] = set()
        self._lock = asyncio.Lock()

    async def start(self):
        """Startup sweep: retry every ``status='printing'`` fallback archive once.

        Called from ``main.py::lifespan`` after dispatch worker is up.
        Safe to call even when nothing needs retrying.
        """
        async with async_session() as db:
            result = await db.execute(
                select(PrintArchive.id)
                .where(or_(PrintArchive.file_path == "", PrintArchive.file_path.is_(None)))
                .where(PrintArchive.status == "printing")
            )
            archive_ids = [row[0] for row in result.all()]

        if not archive_ids:
            return

        logger.info(
            "Archive download retry: startup sweep — %d printing fallback archive(s) to try",
            len(archive_ids),
        )
        for archive_id in archive_ids:
            await self.retry_archive(archive_id)  # status ignored — logged inside

    async def retry_printer_archives(self, printer_id: int) -> int:
        """Retry every ``status='printing'`` fallback archive for *printer_id*.

        Returns the number of archives that successfully got their 3MF.
        Called from the printer-connect hook.
        """
        async with async_session() as db:
            result = await db.execute(
                select(PrintArchive.id)
                .where(PrintArchive.printer_id == printer_id)
                .where(or_(PrintArchive.file_path == "", PrintArchive.file_path.is_(None)))
                .where(PrintArchive.status == "printing")
            )
            archive_ids = [row[0] for row in result.all()]

        if not archive_ids:
            return 0

        recovered = 0
        for archive_id in archive_ids:
            if await self.retry_archive(archive_id) == "recovered":
                recovered += 1
        return recovered

    async def retry_archive(self, archive_id: int) -> RetryStatus:
        """Attempt a single 3MF download for one archive.

        Returns one of ``RetryStatus``.  Safe to call concurrently — a
        second caller while one is in flight gets ``"in_progress"``
        immediately (no queuing, no duplicate FTP sessions).
        """
        async with self._lock:
            if archive_id in self._in_progress:
                logger.info(
                    "retry_archive: archive %s already has a retry in progress — skipping",
                    archive_id,
                )
                return "in_progress"
            self._in_progress.add(archive_id)

        try:
            return await self._do_retry(archive_id)
        finally:
            async with self._lock:
                self._in_progress.discard(archive_id)

    async def _do_retry(self, archive_id: int) -> RetryStatus:
        """Inner retry logic — callers should hold the per-archive guard."""
        async with async_session() as db:
            archive = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
            if archive is None:
                logger.warning("retry_archive: archive %s not found", archive_id)
                return "error"
            if archive.file_path:
                # Already has a file — nothing to do.
                return "already_has_file"
            if archive.printer_id is None:
                logger.warning("retry_archive: archive %s has no printer_id", archive_id)
                return "error"

            printer = (await db.execute(select(Printer).where(Printer.id == archive.printer_id))).scalar_one_or_none()
            if printer is None:
                logger.warning("retry_archive: printer for archive %s not found", archive_id)
                return "error"

            meta = archive.extra_data or {}
            print_data = meta.get("_print_data") or {}
            subtask_name = print_data.get("subtask_name") or meta.get("original_subtask")
            filename = print_data.get("filename") or archive.filename

        logger.info(
            "Archive retry: archive %s (printer %s, subtask=%s)",
            archive_id,
            printer.name,
            subtask_name,
        )

        temp_dir: Path = settings.archive_dir / "temp"
        download_result = await try_download_3mf(printer, subtask_name, filename, temp_dir)
        if not download_result:
            logger.info("Archive retry: archive %s — download failed", archive_id)
            return "failed"

        temp_path, downloaded_filename = download_result
        try:
            async with async_session() as db2:
                service = ArchiveService(db2)
                ok = await service.attach_3mf_to_archive(archive_id, temp_path, downloaded_filename)
                if ok:
                    # Re-fetch with the freshly-attached file_path + push
                    # printable_objects into MQTT state. This is the missing
                    # bridge that lets the skip-objects modal work for prints
                    # started directly from the printer (chicken-and-egg —
                    # the modal's gate condition needs printable_objects_count
                    # > 0, but until this hook runs, that count stays 0 and
                    # the frontend never asks for objects).
                    refreshed = (
                        await db2.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                    ).scalar_one_or_none()
                    if refreshed is not None and archive.printer_id is not None:
                        from backend.app.services.archive import load_objects_from_archive_into_state

                        load_objects_from_archive_into_state(refreshed, archive.printer_id)
            if ok:
                logger.info("Archive retry: recovered 3MF for archive %s", archive_id)
                await ws_manager.send_archive_updated({"id": archive_id, "recovered_3mf": True})
                return "recovered"
            else:
                logger.warning("Archive retry: attach failed for archive %s", archive_id)
                return "failed"
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass


archive_download_retry = ArchiveDownloadRetryService()
