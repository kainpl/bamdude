"""Who owns the work that writes archive videos, so a restore can prove it has stopped (G10 C4).

A restore replaces the database and the archive files. Until now nothing told it
about a video being attached, converted or removed at that moment: those ran as
detached background tasks or inside a request, the restore drained thirteen
other owners and none of these, and a publication could land its file in a tree
that was being swapped out.

Every such writer now runs as an operation this runtime OWNS:

* it is registered before its first await and refused once admission is closed
  (:class:`MediaAdmissionClosed`);
* the work is a task of the runtime, awaited through a shield — a caller that is
  cancelled (a closed browser tab, a cancelled scan) does not detach it: it runs
  to its end and stays registered until then, and the cancelled caller is held
  until that end too. The work uses the caller's database session, and a caller
  that unwound at once would close that session under it;
* :meth:`quiesce` closes admission first and then waits for every operation to
  really end. It does not cancel them: a conversion's ffmpeg child would outlive
  a cancelled task, and "the task is gone" would be a false proof. An operation
  still running when the wait is over refuses the restore.

Only lifespan startup reopens admission, the same rule as the printer file
runtime: a refused drain leaves nothing safe to reopen onto.

Not a task queue and not a process owner. The ffmpeg children of the editor and
the layer stitch get their owner in units E and F; here a conversion is simply
waited for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable

logger = logging.getLogger(__name__)


class MediaAdmissionClosed(RuntimeError):
    """A restore (or shutdown) has closed admission: no new video work starts."""


class ArchiveMediaRuntime:
    """Admission and the registry of every archive-video operation still running."""

    def __init__(self) -> None:
        self.closed = False
        self._tasks: dict[asyncio.Task, tuple[str, float]] = {}

    def admitting(self) -> bool:
        return not self.closed

    def close_admission(self) -> None:
        self.closed = True

    def in_flight(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    def running(self) -> list[str]:
        """Names of the operations still running — for the log and a refusal's explanation."""
        return sorted(name for task, (name, _started) in self._tasks.items() if not task.done())

    def reopen(self) -> None:
        """Only lifespan startup reopens; with an operation still running there is nothing safe to reopen onto."""
        if self.in_flight():
            raise RuntimeError("archive media operations are still running")
        self.closed = False

    async def own(self, name: str, work: Awaitable):
        """Run ``work`` as an operation of this runtime and return its result.

        Refused when admission is closed. The caller may be cancelled; the work
        is not — it finishes, and is counted, either way, and the cancellation
        reaches the caller only after it has.
        """
        if self.closed:
            if asyncio.iscoroutine(work):
                work.close()  # never started: nothing to clean up, and no "never awaited" warning
            raise MediaAdmissionClosed(name)
        task = asyncio.ensure_future(work)
        self._tasks[task] = (name, time.monotonic())
        task.add_done_callback(self._finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Our caller was cancelled (or the work itself was). Wait the work out —
            # repeated cancellation included — and only then let the cancel through.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 — the work's own failure; the caller is being cancelled
                    break
            raise

    def _finished(self, task: asyncio.Task) -> None:
        name, started = self._tasks.pop(task, ("?", time.monotonic()))
        if task.cancelled():
            outcome = "cancelled"
        elif task.exception() is not None:
            outcome = f"failed ({type(task.exception()).__name__})"
        else:
            outcome = "finished"
        logger.debug("[MEDIA] %s %s after %.1fs", name, outcome, time.monotonic() - started)

    async def quiesce(self, seconds: float = 30.0) -> bool:
        """Close admission and wait for every operation to end. False = one is still running."""
        self.close_admission()
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=seconds)
        remaining = self.running()
        if remaining:
            logger.warning(
                "[MEDIA] %s archive video operation(s) still running after %.0fs: %s",
                len(remaining),
                seconds,
                ", ".join(remaining),
            )
            return False
        return True


archive_media_runtime = ArchiveMediaRuntime()
