"""Auto-delete 3MF files of stale archives to reclaim disk space.

Runs once a day at server-local midnight. Removes the physical 3MF
(and its ``.gcode.md5`` sidecars + the per-archive directory if empty
after) for archive groups whose newest print is older than the
operator-configured retention window.

Design choices (per user spec, 2026-04-25):

* **Group by design hash, not per-archive row.** Reprints share a
  ``COALESCE(source_content_hash, content_hash)`` group; the cutoff is
  evaluated against the group's newest ``completed_at``/``created_at``,
  not each row's own timestamp. So if a model was last printed 5 days
  ago, none of its archive rows lose their 3MF — even rows that
  themselves date from months ago.

* **Thumbnails are preserved.** Only ``file_path`` is wiped (set to
  ``""``) and the file deleted. ``thumbnail_path`` and all other
  metadata stay so the archive page still renders the print history
  correctly. The same model used by the fallback-archive flow
  (``file_path=""`` + ``no_3mf_available`` marker) — UI already knows
  how to show "3MF unavailable" rows.

* **Multiple rows per design get cleared together.** When dispatcher
  dedup created multiple archive rows pointing to a shared bytes-on-
  disk file (or to per-row patched copies), all of them in the group
  get their ``file_path`` blanked in one transaction. A single physical
  file on disk is deleted once.

* **Skip rules.** A row is exempted when:

  - ``status='printing'`` — there's a live print referencing the file.
  - An active queue item references the row (any
    ``PrintQueueItem.archive_id == archive.id`` with status in
    {pending, printing, paused}).
  - The originating ``LibraryFile`` still exists with a matching
    ``file_hash`` — that file is the operator-visible source of truth
    for re-printing, no need to wipe a copy in archive when the
    library still has it. (Library has its own retention controls.)

  If any row in a group is exempt, the **whole group** is skipped
  (deleting some copies but not others would leave inconsistent
  ``file_path`` for the same source hash).

* **Daily, server-local midnight.** ``asyncio.sleep`` until next 00:00
  in the host's local time. No cron expression configurability —
  retention windows are coarse-grained (days), the exact hour doesn't
  matter operationally.

* **Minimum 1 day.** Validation in the settings schema; the runner
  defends in depth by clamping anything < 1 to 1.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings
from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CleanupRunResult:
    """Outcome of one cleanup sweep — exposed via ``/archives/cleanup/*``."""

    started_at: datetime
    finished_at: datetime | None = None
    groups_scanned: int = 0
    groups_skipped_active_print: int = 0
    groups_skipped_queue: int = 0
    groups_skipped_library: int = 0
    groups_cleared: int = 0
    archives_cleared: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "groups_scanned": self.groups_scanned,
            "groups_skipped_active_print": self.groups_skipped_active_print,
            "groups_skipped_queue": self.groups_skipped_queue,
            "groups_skipped_library": self.groups_skipped_library,
            "groups_cleared": self.groups_cleared,
            "archives_cleared": self.archives_cleared,
            "bytes_freed": self.bytes_freed,
            "errors": list(self.errors),
        }


LAST_RUN_KEY = "archive_3mf_cleanup_last_run"
# Persist the result alongside the timestamp so server restarts between the
# auto-tick and the next status read don't downgrade the UI to "count was
# lost". Zero is a valid count ("ran, found nothing eligible") and is shown
# as such after restart.
LAST_RUN_ARCHIVES_CLEARED_KEY = "archive_3mf_cleanup_last_archives_cleared"
LAST_RUN_BYTES_FREED_KEY = "archive_3mf_cleanup_last_bytes_freed"
# Auto-tick cadence + period (mirrors library auto-purge).
TICK_INTERVAL_SECONDS = 900
AUTO_PERIOD_HOURS = 24


class ArchiveCleanupService:
    """Background drift-mode loop + on-demand "run now" hook.

    The loop ticks every ``TICK_INTERVAL_SECONDS`` (15 min) and runs the
    sweep at most once per ``AUTO_PERIOD_HOURS`` (24h) — anchored to the
    *previous successful run* (persisted in ``settings.LAST_RUN_KEY``)
    rather than wall-clock midnight. Manual ``run_now()`` calls also stamp
    ``LAST_RUN_KEY`` so the auto-cycle resets after an ad-hoc run.
    """

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_result: CleanupRunResult | None = None
        # Guard against an admin clicking "Run now" while the auto-tick
        # is mid-sweep.  Both paths await the same lock.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Spawn the daily loop. Idempotent."""
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="archive-cleanup-daily")
        logger.info("[ARCHIVE-CLEANUP] daily loop started")

    async def stop(self) -> None:
        """Signal the loop to exit and wait for it. Cancels the task if it
        isn't responsive within a short grace window."""
        self._stop.set()
        task = self._task
        self._task = None
        if not task:
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown best-effort
                pass

    # --------------------------------------------------------------- public api

    @property
    def last_result(self) -> CleanupRunResult | None:
        return self._last_result

    async def get_status(self) -> dict[str, Any]:
        """Return ``{enabled, days, last_run, next_run_at}`` for the UI.

        ``last_run`` mirrors the in-memory ``CleanupRunResult`` when one
        exists. After a process restart that result is gone but the
        persisted ``LAST_RUN_KEY`` timestamp may still be there — in that
        case we synthesize a result with ``archives_cleared=-1`` so the UI
        knows "we know it ran but the count is gone, see logs".

        ``next_run_at`` is the earliest moment the auto-tick can fire:
        ``last + 24h`` (clamped to now) when ``LAST_RUN_KEY`` exists, or
        ``now + tick_interval`` for never-run-yet enabled state. NULL when
        auto-mode is off.
        """
        async with async_session() as db:
            enabled, days = await self._read_settings(db)
            last_persisted = await self._read_last_run(db)
            last_archives_persisted = await self._read_int_setting(db, LAST_RUN_ARCHIVES_CLEARED_KEY)
            last_bytes_persisted = await self._read_int_setting(db, LAST_RUN_BYTES_FREED_KEY)

        last_run: dict[str, Any] | None = None
        if self._last_result is not None:
            last_run = self._last_result.as_dict()
        elif last_persisted is not None:
            ts = last_persisted.isoformat()
            # Server restarted between the run and now. Pull the count from
            # the persisted setting if it's there (writes by ``_run_once``
            # since the introduction of LAST_RUN_ARCHIVES_CLEARED_KEY); fall
            # back to the -1 sentinel only for legacy stamps that pre-date it.
            last_run = {
                "started_at": ts,
                "finished_at": ts,
                "groups_scanned": 0,
                "groups_skipped_active_print": 0,
                "groups_skipped_queue": 0,
                "groups_skipped_library": 0,
                "groups_cleared": 0,
                "archives_cleared": last_archives_persisted if last_archives_persisted is not None else -1,
                "bytes_freed": last_bytes_persisted if last_bytes_persisted is not None else 0,
                "errors": [],
            }

        next_run_at: datetime | None = None
        if enabled:
            now = datetime.now(timezone.utc)
            if last_persisted is None:
                next_run_at = now + timedelta(seconds=TICK_INTERVAL_SECONDS)
            else:
                candidate = last_persisted + timedelta(hours=AUTO_PERIOD_HOURS)
                next_run_at = candidate if candidate > now else now

        return {
            "enabled": enabled,
            "days": days,
            "last_run": last_run,
            "next_run_at": next_run_at.isoformat() if next_run_at else None,
        }

    async def run_now(self, override_days: int | None = None) -> CleanupRunResult:
        """Trigger one sweep immediately, regardless of the daily cron.

        Returns the run summary; same shape as ``last_result`` after the
        scheduled tick.  Bypasses the ``enabled`` setting (which only gates
        the daily auto-tick). Pass ``override_days`` to use an ad-hoc
        threshold instead of the saved ``archive_3mf_retention_days``.
        """
        async with self._lock:
            return await self._run_once(override_days=override_days)

    async def preview(self, override_days: int | None = None) -> dict[str, Any]:
        """Dry-run: compute what *would* be cleared right now without
        touching disk or DB.  Used by the settings UI + Archives modal to
        show "X archives, Y MB to free" before the operator commits.

        Always computes — the ``enabled`` flag in settings only gates the
        daily auto-tick. Manual ``Run now`` (and this preview) honours the
        configured ``days`` threshold (or ``override_days`` when supplied)
        regardless. The returned ``enabled`` field is informational so the
        UI can hint "auto-mode is off, but a manual run will still fire"
        without disabling the button.
        """
        async with async_session() as db:
            enabled, settings_days = await self._read_settings(db)
            days = max(1, int(override_days)) if override_days is not None else settings_days
            cutoff = self._cutoff_utc(days)
            plan = await self._plan_groups(db, cutoff, dry_run=True)
        return {
            "enabled": enabled,
            "days": days,
            "cutoff": cutoff.isoformat(),
            "groups": plan["cleared_groups"],
            "archives": plan["cleared_archives"],
            "bytes": plan["bytes"],
        }

    # ---------------------------------------------------------------- auto-tick loop

    async def _loop(self) -> None:
        """Tick every 15 min, run the sweep when 24h has elapsed since
        the previous successful run (auto + manual both count).

        Drift-mode anchored to ``LAST_RUN_KEY`` rather than wall-clock
        midnight — matches the library auto-purge cadence so the two bins
        behave identically. ``enabled=False`` skips the run; ``last_run``
        timestamp is NOT consumed by skip ticks, so the next time auto is
        re-enabled the gap-check still sees the genuine last run.
        """
        while not self._stop.is_set():
            try:
                # Tick first, then evaluate — same shape as the library
                # trash sweeper. Sleep in 60 s chunks so the stop event
                # breaks us out promptly on shutdown.
                deadline = asyncio.get_running_loop().time() + TICK_INTERVAL_SECONDS
                while not self._stop.is_set():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=min(remaining, 60.0))
                    except asyncio.TimeoutError:
                        continue
                    else:
                        return  # stop was set
                if self._stop.is_set():
                    return

                # Drift-mode auto-tick: gated by ``enabled`` + 24h window.
                # Manual ``run_now()`` bypasses this gate but still stamps
                # ``LAST_RUN_KEY``, so a manual run resets the auto-cycle.
                async with async_session() as db:
                    enabled, _days = await self._read_settings(db)
                    if not enabled:
                        logger.debug("[ARCHIVE-CLEANUP] auto-mode disabled, skipping tick")
                        continue
                    last = await self._read_last_run(db)

                if last is not None and (datetime.now(timezone.utc) - last) < timedelta(hours=AUTO_PERIOD_HOURS):
                    continue

                async with self._lock:
                    await self._run_once()

            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover — defensive
                logger.exception("[ARCHIVE-CLEANUP] loop iteration failed: %s", e)
                # Avoid a tight error spin: sleep 60 s before next attempt.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    continue
                else:
                    return

    @staticmethod
    async def _read_last_run(db: AsyncSession) -> datetime | None:
        from backend.app.api.routes.settings import get_setting

        raw = await get_setting(db, LAST_RUN_KEY)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _read_int_setting(db: AsyncSession, key: str) -> int | None:
        from backend.app.api.routes.settings import get_setting

        raw = await get_setting(db, key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    async def _stamp_last_run(when: datetime, archives_cleared: int, bytes_freed: int) -> None:
        """Persist ``LAST_RUN_KEY`` + the result count so subsequent auto-ticks
        skip until 24h has passed AND a server restart between the run and
        the next status read can show the real count instead of the
        ``-1`` "lost on restart" sentinel. Called from ``_run_once`` so
        both auto and manual paths push the timer forward and persist the
        count uniformly.
        """
        from backend.app.models.settings import Settings

        async with async_session() as db:
            iso = when.isoformat()
            for key, value in (
                (LAST_RUN_KEY, iso),
                (LAST_RUN_ARCHIVES_CLEARED_KEY, str(int(archives_cleared))),
                (LAST_RUN_BYTES_FREED_KEY, str(int(bytes_freed))),
            ):
                result = await db.execute(select(Settings).where(Settings.key == key))
                row = result.scalar_one_or_none()
                if row is None:
                    db.add(Settings(key=key, value=value))
                else:
                    row.value = value
            await db.commit()

    # ---------------------------------------------------------------- core run

    async def _run_once(self, override_days: int | None = None) -> CleanupRunResult:
        """Execute one sweep using ``override_days`` or the saved threshold.

        Does NOT consult the ``enabled`` setting — caller decides whether to
        respect it. The daily loop checks ``enabled`` before invoking us;
        manual ``run_now()`` deliberately bypasses the gate so admins can
        clean up on demand without flipping the auto-mode toggle.

        ``override_days`` lets manual callers (e.g. the Archives page
        modal) pass an ad-hoc threshold without touching the saved value.
        """
        result = CleanupRunResult(started_at=datetime.now(timezone.utc))
        try:
            async with async_session() as db:
                _enabled, settings_days = await self._read_settings(db)
                days = max(1, int(override_days)) if override_days is not None else settings_days
                cutoff = self._cutoff_utc(days)
                logger.info(
                    "[ARCHIVE-CLEANUP] sweep starting (retention=%d days, cutoff=%s)",
                    days,
                    cutoff.isoformat(),
                )
                plan = await self._plan_groups(db, cutoff, dry_run=False)
                result.groups_scanned = plan["groups_count"]
                result.groups_skipped_active_print = plan["skipped_active"]
                result.groups_skipped_queue = plan["skipped_queue"]
                result.groups_skipped_library = plan["skipped_library"]
                result.groups_cleared = plan["cleared_groups"]
                result.archives_cleared = plan["cleared_archives"]
                result.bytes_freed = plan["bytes"]
                result.errors.extend(plan["errors"])
        except Exception as e:  # pragma: no cover — defensive
            logger.exception("[ARCHIVE-CLEANUP] sweep crashed: %s", e)
            result.errors.append(f"{type(e).__name__}: {e}")
        finally:
            result.finished_at = datetime.now(timezone.utc)
            self._last_result = result
            # Both auto-ticks and manual ``run_now()`` calls land here, so
            # stamping unconditionally means a manual run also resets the
            # 24h auto-cycle. Failed sweeps still stamp — re-trying every
            # 15 min on a permanent error would spam the loop. Operators
            # see the error in the result + logs and can fix root cause.
            try:
                await self._stamp_last_run(result.started_at, result.archives_cleared, result.bytes_freed)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("[ARCHIVE-CLEANUP] failed to stamp last_run: %s", exc)
            logger.info(
                "[ARCHIVE-CLEANUP] sweep done: cleared %d group(s), %d archive(s), %s bytes",
                result.groups_cleared,
                result.archives_cleared,
                result.bytes_freed,
            )
        return result

    @staticmethod
    async def _read_settings(db: AsyncSession) -> tuple[bool, int]:
        """Pull (enabled, days) from app settings; clamp days to >= 1."""
        from backend.app.api.routes.settings import get_setting

        enabled_raw = (await get_setting(db, "archive_3mf_retention_enabled") or "false").lower()
        enabled = enabled_raw == "true"
        try:
            days = int(await get_setting(db, "archive_3mf_retention_days") or "30")
        except (TypeError, ValueError):
            days = 30
        return enabled, max(1, days)

    @staticmethod
    def _cutoff_utc(days: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=days)

    # -------------------------------------------------------------- plan + apply

    async def _plan_groups(
        self,
        db: AsyncSession,
        cutoff: datetime,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Walk archives grouped by design-hash, decide who to wipe.

        Logic:

        1. SQL-side pre-filter: ``GROUP BY effective_hash`` over rows with
           a populated ``file_path``, count groups + find the newest
           timestamp per group. Only buckets whose newest timestamp is
           older than ``cutoff`` get loaded into Python — saves memory
           on installs with thousands of archives where most groups are
           hot.
        2. Load the full ``PrintArchive`` rows for those expired buckets
           in one query.
        3. Per bucket, check skip rules; if any row vetoes, skip the
           whole bucket (don't half-clear a design — leaving some copies
           on disk while wiping others is worse than not cleaning at all).
        4. Otherwise apply the cleanup transactionally: delete files,
           blank ``file_path`` on every row in the bucket, commit.

        ``dry_run=True`` exits before step 4 — used by the preview
        endpoint before the operator commits to "Run now".

        Bucket key uses ``COALESCE(source_content_hash, content_hash)``
        — defence-in-depth around the always-fill invariant (m039+).
        Rows without a usable hash (theoretical post-m039) get a
        unique singleton key so they're still considered individually.
        """
        # Stats we need: total bucket count for telemetry. One round-trip
        # to count all buckets (hot or cold).
        eff_hash_col = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
        total_groups = (
            await db.execute(
                select(func.count(func.distinct(eff_hash_col))).where(
                    PrintArchive.file_path.is_not(None),
                    PrintArchive.file_path != "",
                )
            )
        ).scalar() or 0

        # Newest activity per bucket. ``GREATEST`` would be cleaner but
        # SQLite doesn't have it; emulate with nested COALESCE — same
        # priority order as the docstring on ``CleanupRunResult``.
        newest_ts = func.max(func.coalesce(PrintArchive.completed_at, PrintArchive.started_at, PrintArchive.created_at))
        expired_keys_rows = (
            await db.execute(
                select(eff_hash_col)
                .where(PrintArchive.file_path.is_not(None), PrintArchive.file_path != "")
                .group_by(eff_hash_col)
                .having(newest_ts < cutoff)
            )
        ).all()
        expired_keys = [row[0] for row in expired_keys_rows if row[0] is not None]

        if not expired_keys:
            return {
                "groups_count": total_groups,
                "skipped_active": 0,
                "skipped_queue": 0,
                "skipped_library": 0,
                "cleared_groups": 0,
                "cleared_archives": 0,
                "bytes": 0,
                "errors": [],
            }

        # Now load only the rows in expired buckets — usually a tiny
        # fraction of the table on a healthy install.
        rows = (
            (
                await db.execute(
                    select(PrintArchive)
                    .where(PrintArchive.file_path.is_not(None), PrintArchive.file_path != "")
                    .where(eff_hash_col.in_(expired_keys))
                )
            )
            .scalars()
            .all()
        )

        # Bucket by design hash — same key as the SQL-side filter.
        buckets: dict[str, list[PrintArchive]] = {}
        for row in rows:
            key = row.source_content_hash or row.content_hash or f"__nohash__:{row.id}"
            buckets.setdefault(key, []).append(row)

        skipped_active = 0
        skipped_queue = 0
        skipped_library = 0
        cleared_groups = 0
        cleared_archives = 0
        bytes_freed = 0
        errors: list[str] = []

        for bucket_key, members in buckets.items():
            newest = self._bucket_newest_ts(members)
            if newest is None or newest >= cutoff:
                # Either we couldn't find a usable timestamp (defensive)
                # or the design is still within the retention window.
                continue

            skip_reason = await self._bucket_skip_reason(db, members)
            if skip_reason == "active_print":
                skipped_active += 1
                continue
            if skip_reason == "queue":
                skipped_queue += 1
                continue
            if skip_reason == "library":
                skipped_library += 1
                continue

            # Plan the byte counts; for dry_run we only sum, not delete.
            unique_paths: dict[str, int] = {}
            for member in members:
                if not member.file_path:
                    continue
                disk = self._resolve_disk_path(member.file_path)
                if disk and disk.is_file():
                    unique_paths[str(disk)] = unique_paths.get(str(disk), 0) or disk.stat().st_size

            group_bytes = sum(unique_paths.values())

            if dry_run:
                cleared_groups += 1
                cleared_archives += len(members)
                bytes_freed += group_bytes
                continue

            try:
                actually_freed = await self._clear_bucket(db, members, unique_paths)
                cleared_groups += 1
                cleared_archives += len(members)
                bytes_freed += actually_freed
                logger.info(
                    "[ARCHIVE-CLEANUP] cleared bucket %s — %d archive(s), %d bytes",
                    bucket_key[:32],
                    len(members),
                    actually_freed,
                )
            except Exception as e:  # pragma: no cover — keep sweep going on partial errors
                logger.exception("[ARCHIVE-CLEANUP] failed to clear bucket %s: %s", bucket_key[:32], e)
                errors.append(f"bucket {bucket_key[:16]}: {type(e).__name__}: {e}")

        return {
            "groups_count": total_groups,
            "skipped_active": skipped_active,
            "skipped_queue": skipped_queue,
            "skipped_library": skipped_library,
            "cleared_groups": cleared_groups,
            "cleared_archives": cleared_archives,
            "bytes": bytes_freed,
            "errors": errors,
        }

    @staticmethod
    def _bucket_newest_ts(members: list[PrintArchive]) -> datetime | None:
        candidates: list[datetime] = []
        for m in members:
            for ts in (m.completed_at, m.started_at, m.created_at):
                if ts is not None:
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    candidates.append(ts)
        if not candidates:
            return None
        return max(candidates)

    @staticmethod
    async def _bucket_skip_reason(db: AsyncSession, members: list[PrintArchive]) -> str | None:
        """Return ``"active_print" | "queue" | "library" | None``.

        Returning a non-None reason vetoes the whole bucket — no row in
        the design group has its 3MF removed. Mirrors the user spec:
        if a printable copy is still in active use anywhere we should
        not start chipping away at the design's bytes, even if some
        copies are technically eligible.
        """
        # Active print on any member.
        for m in members:
            if m.status == "printing":
                return "active_print"

        # Queue items still referencing any member.
        ids = [m.id for m in members]
        if ids:
            queue_match = await db.execute(
                select(PrintQueueItem.id)
                .where(PrintQueueItem.archive_id.in_(ids))
                .where(PrintQueueItem.status.in_(("pending", "printing", "paused")))
                .limit(1)
            )
            if queue_match.scalar_one_or_none() is not None:
                return "queue"

        # Library file still present with the design's hash → keep.
        # First: any library_file_id linked to the bucket. If the row is
        # still in library_files we treat the design as "kept by library"
        # and skip cleanup — the library is the operator's deliberate
        # storage, archive duplicates are fair game otherwise.
        lib_ids = {m.library_file_id for m in members if m.library_file_id is not None}
        if lib_ids:
            lib_match = await db.execute(select(LibraryFile.id).where(LibraryFile.id.in_(lib_ids)).limit(1))
            if lib_match.scalar_one_or_none() is not None:
                return "library"

        # Hash-based fallback: even without library_file_id we can match
        # by source_content_hash — after always-fill (m039) this is the
        # canonical chain root that pairs with ``LibraryFile.file_hash``.
        # ``content_hash`` is intentionally excluded: a patched archive's
        # content_hash never equals any library file's hash anyway, so
        # adding it just slows the IN clause without finding new matches.
        source_hashes = {m.source_content_hash for m in members if m.source_content_hash}
        if source_hashes:
            lib_match = await db.execute(
                select(LibraryFile.id).where(LibraryFile.file_hash.in_(source_hashes)).limit(1)
            )
            if lib_match.scalar_one_or_none() is not None:
                return "library"

        return None

    @staticmethod
    def _resolve_disk_path(file_path: str) -> Path | None:
        try:
            p = Path(file_path)
            return p if p.is_absolute() else app_settings.base_dir / p
        except (TypeError, OSError):
            return None

    @staticmethod
    async def _clear_bucket(
        db: AsyncSession,
        members: list[PrintArchive],
        unique_paths: dict[str, int],
    ) -> int:
        """Delete files on disk, blank ``file_path`` on every member.

        Returns the actual byte total freed (post-stat, as files may
        have been removed by other means between plan and apply).

        Thumbnails are intentionally preserved — only the 3MF + its
        ``.gcode.md5`` sidecar are removed. The per-archive directory
        is removed if it ends up empty (no thumbnail, no other files).
        """
        actually_freed = 0
        for path_str in unique_paths:
            disk = Path(path_str)
            try:
                if disk.is_file():
                    actually_freed += disk.stat().st_size
                    disk.unlink()
                # Sidecars: ``X.gcode.md5`` next to ``X.3mf``.
                for sidecar in disk.parent.glob(f"{disk.name}*.md5"):
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
                # Drop the per-archive folder if it's now empty (no
                # thumbnail, no leftover files). ``rmdir`` only succeeds
                # when the directory is truly empty, so this is safe.
                parent = disk.parent
                try:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    pass
            except FileNotFoundError:
                # Already gone — count zero, blank the row anyway.
                continue
            except OSError as e:
                logger.warning("[ARCHIVE-CLEANUP] could not delete %s: %s", disk, e)

        ids = [m.id for m in members]
        if ids:
            await db.execute(update(PrintArchive).where(PrintArchive.id.in_(ids)).values(file_path=""))
            await db.commit()

        return actually_freed


archive_cleanup_service = ArchiveCleanupService()
