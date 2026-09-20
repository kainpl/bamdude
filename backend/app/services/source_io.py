"""Bounded, read-only source probes; a dead network mount must not own the event loop.

OS filesystem calls cannot be cancelled. Timed-out probes keep their slot and
are reused until they finish; they never fill asyncio's shared executor or
create an unbounded backlog. Daemon workers are not joined by executor shutdown.
Never submit writes, DB work or printer commands here: a probe may finish late.
"""

import asyncio
import time
from concurrent.futures import Future
from threading import Lock, Thread

SOURCE_IO_TIMEOUT = 5.0
_MAX_PROBES = 4
_lock = Lock()
_running: dict[tuple, tuple[Future, float]] = {}
SOURCE_FAILURES = frozenset({"source_unreadable", "source_timeout"})


class SourceUnavailable(Exception):
    revision = None

    def __init__(self, reason="source_unreadable"):
        self.reason = reason
        super().__init__(reason)


async def source_probe(key, operation, *args, **kwargs):
    """Run a read-only operation with a deadline, coalescing outstanding probes."""
    with _lock:
        running = _running.get(key)
        if running is None:
            if len(_running) >= _MAX_PROBES:
                # Capacity is not evidence that this particular file is broken.
                raise SourceUnavailable("source_check_busy")
            future = Future()
            deadline = time.monotonic() + SOURCE_IO_TIMEOUT
            _running[key] = (future, deadline)

            def work():
                try:
                    result = operation(*args, **kwargs)
                except BaseException as exc:
                    with _lock:
                        _running.pop(key, None)
                    future.set_exception(exc)
                else:
                    with _lock:
                        _running.pop(key, None)
                    future.set_result(result)

            Thread(target=work, name="source-probe", daemon=True).start()
        else:
            future, deadline = running
    remaining = deadline - time.monotonic()
    try:
        # A completed read must not fail merely because the event loop was
        # scheduled late while starting its worker.
        if future.done():
            return future.result()
        if remaining <= 0:
            raise SourceUnavailable("source_timeout")
        # wait() leaves the wrapped future alive on timeout/cancellation, and
        # consumes any late exception without an orphaned asyncio warning.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        done, _ = await asyncio.wait({wrapped}, timeout=remaining)
        if not done:
            raise SourceUnavailable("source_timeout")
        return wrapped.result()
    except OSError as exc:
        raise SourceUnavailable() from exc


async def require_source_file(path):
    if not await source_probe(("is_file", str(path)), path.is_file):
        raise SourceUnavailable()
