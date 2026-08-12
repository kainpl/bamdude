"""Background-task helper that keeps a strong reference to fire-and-forget tasks.

asyncio holds only a weak reference to tasks returned by ``create_task`` --
when the caller discards the return value (the "fire and forget" pattern),
the task can be garbage-collected mid-execution and the event loop logs
``Task was destroyed but it is pending!`` with no traceback. A support
bundle review under #1648 surfaced 94 such warnings in 8 days of v0.2.4.5.

``spawn_background_task`` stores the task in a module-level set, removes it
when the task completes, and surfaces any uncaught exception through the
logger, so a silently-swallowed error becomes a visible WARNING with the
originating traceback instead of an opaque GC warning.

Use this for any work that should run in the background without being
awaited inline. For tasks that the service owns and needs to cancel on
shutdown, store the returned ``asyncio.Task`` on the service instance
instead (the helper still adds the strong reference, so storing it twice
is redundant but harmless).

⚠️ **This is the intended route, not the only one taken.** This docstring
used to claim ``spawn_background_task`` was "the one place in the codebase
that calls ``asyncio.create_task``"; measured 2026-08-13, there are 66 raw
``create_task`` call sites under ``backend/app`` and exactly one of them is
here. Whether each of the other 65 keeps its own reference and
cancels on shutdown — which the paragraph above explicitly permits — has not
been audited, so treat that number as a list to check rather than a count of
leaks. Re-measure before trusting it:

    rg -c 'asyncio\\.create_task\\(' backend/app

The claim was left standing long enough to be believed, which is the whole
reason it is written down this way now: a rule and its observed compliance
are two different facts, and only one of them was ever true here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Strong-reference holder. Tasks live here from creation through completion.
# Module-level so the set survives across spawn calls; the done-callback
# removes each task as it finishes so the set doesn't grow without bound
# (the event loop's GC can't reap an entry the callback still holds, but
# the discard breaks the cycle immediately).
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Schedule ``coro`` on the running loop without losing the task reference.

    Args:
        coro: The coroutine to run. Must not already be a Task.
        name: Optional task name surfaced in /tracebacks and the
            done-callback log line so a leaked task is traceable to its
            spawn site.

    Returns:
        The created ``asyncio.Task``. Most callers ignore it -- the helper
        keeps its own strong reference. Callers that need to ``await`` or
        cancel later can store it on a service instance.
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task[Any]) -> None:
    """Discard the strong reference and surface any uncaught exception.

    Without this, an exception raised inside a fire-and-forget task is
    silently retrieved by ``Task.__del__`` and never reaches the logger.
    Surface it here as a WARNING with the task name so support bundles
    capture the originating error instead of an opaque GC notice.
    """
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "Background task %r raised an uncaught exception",
            task.get_name(),
            exc_info=exc,
        )


def active_task_count() -> int:
    """Number of background tasks currently in flight. Used by tests."""
    return len(_background_tasks)
