"""The process's owner of slicer-connector work: submission coordinators and upload slots (OrcaBamDude spec r2 §8.3-8.4).

- **Coordinators** run as background tasks (``core/tasks.spawn_background_task``), never inside the request: an HTTP
  disconnect does not take a publication with it. One per operation; at most two per API key identity and eight per
  process. They carry scalar identities only and open their own sessions.
- **Upload slots** -- two per process. A slot is held from admission until the work that uses the bytes has really
  ended; a cancelled request does not release it early (the work runs in its own task).
- **Requests** -- every route that reads or writes an operation runs its storage work as a task of its own
  (:meth:`ConnectorRuntime.own_request`): from the proof of ownership to the last write and the answer's last read.
  A restore drains it, and a cancelled request leaves it to finish, so storage is never replaced between a proof and
  what relies on it.
- **Restore.** :meth:`ConnectorRuntime.quiesce_for_restore` closes admission before its first await and drains
  everything owned; it never reopens in this process (a restore needs a restart). Before storage is replaced the
  restore writes a fence file outside the replaced storage; the new database gets a new storage epoch, and the fence
  goes only after that commit. A start that finds the fence finishes the rotation before admission opens, and keeps
  admission closed when it cannot -- the old namespace must never answer for the restored storage.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from backend.app.core.config import settings
from backend.app.core.tasks import spawn_background_task
from backend.app.services import connector_submissions as submissions
from backend.app.services.connector_submissions import ConnectorActor, ConnectorRefusal

logger = logging.getLogger(__name__)

MAX_COORDINATORS_PER_KEY = 2
MAX_COORDINATORS = 8
MAX_UPLOADS = 2
DRAIN_SECONDS = 30
COMPACTION_INTERVAL_SECONDS = 6 * 3600
FENCE_NAME = ".connector-restore-pending"

RESTORING = "The slicer connector is unavailable while storage is being restored."
T = TypeVar("T")
BUSY = "The slicer connector is busy; retry shortly."


def fence_path() -> Path:
    return Path(settings.base_dir) / FENCE_NAME  # SEC-PATH-OK: FENCE_NAME is this module's constant, never input


class ConnectorRuntime:
    def __init__(self):
        self._coordinators: dict[int, tuple[str, asyncio.Task]] = {}
        self._uploads: set[asyncio.Task] = set()
        # Every request's storage work in progress: a restore waits for it before storage is replaced.
        self._requests: set[asyncio.Task] = set()
        self._restore_hold = False
        self._fenced = False
        self._compactor: asyncio.Task | None = None
        # Uvicorn serves no request before the lifespan's start, which checks the fence; shutdown closes admission.
        self._started = True

    # ----------------------------------------------------------------- admission

    @property
    def admitting(self) -> bool:
        return self._started and not self._restore_hold and not self._fenced

    @property
    def restore_fenced(self) -> bool:
        return self._fenced

    def ensure_admitting(self) -> None:
        if not self.admitting:
            raise ConnectorRefusal(503, "storage_restoring", RESTORING, retryable=True)

    async def own_request(self, work: Callable[[], Awaitable[T]], *, new_work: bool) -> T:
        """Run one request's storage work as a task the restore lifecycle owns (H0 reviews r1 and r2).

        Refused before it starts while a restore holds or is fenced -- and, for new work (submit, resume), whenever
        admission is closed. The work runs as a task of its own that ``quiesce_for_restore`` drains, so storage is
        never replaced between a proof of ownership and the write or answer that relies on it. A cancelled request
        (an HTTP disconnect) leaves the task to finish: the lease ends with the work, never with the request.
        """
        if new_work:
            self.ensure_admitting()
        elif self._restore_hold or self._fenced:
            raise ConnectorRefusal(503, "storage_restoring", RESTORING, retryable=True)
        task = asyncio.create_task(work(), name="connector-request")
        self._requests.add(task)
        task.add_done_callback(self._release_request)
        return await asyncio.shield(task)

    def _release_request(self, task: asyncio.Task) -> None:
        self._requests.discard(task)
        if not task.cancelled():
            task.exception()  # a refusal is the request's answer; one nobody awaits any more is no error to log

    def running(self, submission_id: int) -> asyncio.Task | None:
        entry = self._coordinators.get(submission_id)
        return entry[1] if entry is not None and not entry[1].done() else None

    def start(self, submission_id: int, actor: ConnectorActor, *, states=submissions.RESUMABLE) -> asyncio.Task:
        """The operation's coordinator: the running one if there is one, else a new one within the limits."""
        self.ensure_admitting()
        running = self.running(submission_id)
        if running is not None:
            return running
        live = [(identity, task) for identity, task in self._coordinators.values() if not task.done()]
        if len(live) >= MAX_COORDINATORS or sum(i == actor.key_identity for i, _ in live) >= MAX_COORDINATORS_PER_KEY:
            raise ConnectorRefusal(429, "capacity_exceeded", BUSY, retryable=True)
        task = spawn_background_task(
            submissions.run(submission_id, actor, states=states), name=f"connector-submission-{submission_id}"
        )
        self._coordinators[submission_id] = (actor.key_identity, task)

        def _forget(done: asyncio.Task) -> None:
            current = self._coordinators.get(submission_id)
            if current is not None and current[1] is done:
                del self._coordinators[submission_id]

        task.add_done_callback(_forget)
        return task

    async def wait(self, task: asyncio.Task, timeout: float) -> bool:
        """Wait for a coordinator without ever cancelling it: a request that gives up leaves the work running."""
        done, _ = await asyncio.wait({task}, timeout=timeout)
        return bool(done)

    async def run_upload(self, work: Callable[[], Awaitable]):
        """Run owned upload work in one of the process's slots; the slot is freed only when the work has ended."""
        self.ensure_admitting()
        live = {task for task in self._uploads if not task.done()}
        if len(live) >= MAX_UPLOADS:
            raise ConnectorRefusal(429, "capacity_exceeded", BUSY, retryable=True)
        task = spawn_background_task(work(), name="connector-upload")
        self._uploads.add(task)
        task.add_done_callback(self._uploads.discard)
        return await asyncio.shield(task)

    def uploads_full(self) -> bool:
        return sum(not task.done() for task in self._uploads) >= MAX_UPLOADS

    # ----------------------------------------------------------------- lifecycle

    async def _drain(self, timeout: float) -> bool:
        """Wait for every owned task under one deadline. Admission is closed by then, so nothing new joins."""
        pending = {task for _, task in self._coordinators.values()} | self._uploads | self._requests
        pending = {task for task in pending if not task.done()}
        if not pending:
            return True
        _, remaining = await asyncio.wait(pending, timeout=timeout)
        return not remaining

    async def _stop_compactor(self) -> None:
        task, self._compactor = self._compactor, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=DRAIN_SECONDS)

    async def _compaction_loop(self) -> None:
        while True:
            if self.admitting:
                try:
                    count = await submissions.compact()
                    if count:
                        logger.info("Slicer connector: %d finished operation(s) compacted", count)
                except Exception:  # noqa: BLE001 -- the next pass tries again
                    logger.warning("Slicer connector: compaction failed", exc_info=True)
            await asyncio.sleep(COMPACTION_INTERVAL_SECONDS)

    async def quiesce_for_restore(self) -> bool:
        """Close admission before the first await, then drain every owned task. False: something is still running."""
        self._restore_hold = True
        await self._stop_compactor()
        drained = await self._drain(DRAIN_SECONDS)
        if not drained:
            logger.warning("Slicer connector work is still running; restore refused")
        return drained

    def write_restore_fence(self) -> None:
        """Before storage is replaced: a durable mark, outside that storage, that its epoch must change."""
        path = fence_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("restore pending\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._fenced = True

    def clear_restore_fence(self) -> None:
        """A restore that replaced nothing: the fence goes, the old epoch stands. Admission stays held (restart)."""
        fence_path().unlink(missing_ok=True)
        self._fenced = False

    async def finish_restore(self) -> bool:
        """Commit auth invalidation and connector epoch together before removing the fence."""
        try:
            from backend.app.core import database
            from backend.app.core.auth_lifetime import stage_restore_invalidation

            async with database.async_session() as db:
                await stage_restore_invalidation(db)
                await submissions.stage_storage_epoch(db)
                await db.commit()
            fence_path().unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 -- the fence stays; the next start finishes the rotation
            logger.warning("Authentication and connector restore finalization failed; admission remains fenced")
            return False
        logger.info("Authentication and connector restore finalization committed")
        return True

    async def startup(self) -> None:
        """Open admission -- unless a restore's fence is still there and its rotation cannot be finished."""
        self._started = True
        self._restore_hold = False
        if fence_path().exists():
            self._fenced = True
            if await self.finish_restore():
                self._fenced = False
        else:
            self._fenced = False
        if self._compactor is None or self._compactor.done():
            self._compactor = spawn_background_task(self._compaction_loop(), name="connector-compaction")

    async def shutdown(self) -> None:
        self._started = False
        await self._stop_compactor()
        if not await self._drain(DRAIN_SECONDS):
            for task in [task for _, task in self._coordinators.values()] + list(self._requests):
                task.cancel()
            logger.warning("Slicer connector work was still running at shutdown")

    def _reset_for_tests(self) -> None:
        self._coordinators.clear()
        self._uploads.clear()
        self._requests.clear()
        self._restore_hold = self._fenced = False
        self._started = True


connector_runtime = ConnectorRuntime()
