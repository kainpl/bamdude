"""Cancellation-safe ownership of the small, bounded camera cleanup operations."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")
_owned_tasks: set[asyncio.Task] = set()
# Explicit handoff to the existing camera janitor, including Windows and
# external RTSP URLs that its Linux /proc Bambu-signature scan cannot find.
_unreaped_processes: dict[int, asyncio.subprocess.Process] = {}
logger = logging.getLogger(__name__)
_PROCESS_TERMINATE_TIMEOUT = 2.0
_PROCESS_KILL_TIMEOUT = 2.0


def own_task(coroutine: Coroutine[Any, Any, _T], *, name: str) -> asyncio.Task[_T]:
    """Keep unfinished cleanup alive even if its original caller goes away."""
    task = asyncio.create_task(coroutine, name=name)
    _owned_tasks.add(task)

    def finished(task):
        _owned_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved even when every waiter was cancelled

    task.add_done_callback(finished)
    return task


async def await_cleanup(task: asyncio.Task[_T]) -> _T:
    """Finish a bounded, owned operation before propagating caller cancellation.

    Repeated cancel() calls neither reset its deadline nor cancel the operation.
    A cleanup failure must not replace the original caller's CancelledError.
    """
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    if cancelled is not None:
        # own_task retrieves failures, but also support an externally owned task.
        if task.done() and not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


class CameraCleanupError(RuntimeError):
    """A camera attempt cannot safely be followed by another attempt."""


async def stop_camera_process(process, context) -> bool:
    """Bound both graceful termination and reaping after kill."""
    if process.returncode is not None:
        return True
    try:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), _PROCESS_TERMINATE_TIMEOUT)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), _PROCESS_KILL_TIMEOUT)
    except (OSError, TimeoutError):
        pass
    if process.returncode is None:
        logger.error("Camera ffmpeg exit unconfirmed; handing off to orphan janitor [%s pid=%s]", context, process.pid)
        return False
    return True


async def reap_camera_processes() -> int:
    """Retry only processes explicitly left by camera attempts, by Process owner."""
    processes = list(_unreaped_processes.values())
    if not processes:
        return 0
    results = await asyncio.gather(*(stop_camera_process(process, "orphan janitor") for process in processes))
    for process, reaped in zip(processes, results, strict=True):
        if reaped and _unreaped_processes.get(process.pid) is process:
            del _unreaped_processes[process.pid]
    return sum(results)


class CameraAttempt:
    """Own one ffmpeg process, stderr drain and optional TLS proxy.

    Acquisition happens inside ``async with``; cleanup also runs on spawn
    failure, early return and cancellation. A failed close prevents reconnect.
    """

    def __init__(self, context: str, *, stop_process=None):
        self.context = context
        self.process = None
        self.proxy = None
        self.stderr = None
        self.stop_process = stop_process

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        task = own_task(self._close(), name="camera-attempt-close")
        try:
            await await_cleanup(task)
        except asyncio.CancelledError:
            if exc_type is None or not task.cancelled():
                raise
            # Cancellation inside cleanup must not replace an exception already
            # propagating from the attempt. Caller cancellation still propagates.
        except Exception:
            if exc_type is None:
                raise
            # Keep the original failure/cancellation. _close already logged the
            # cleanup failure; neither a traceback nor a credentialed argv here.

    async def _close(self):
        from backend.app.services.camera_tls import close_tls_proxy
        from backend.app.services.ffmpeg_stderr import FfmpegStdoutDrain
        from backend.app.utils.ffmpeg_output import summarize_ffmpeg_stderr

        succeeded = True
        stdout_drain = None
        try:
            if self.process is not None:
                # The caller has stopped reading stdout before it exits this
                # attempt.  Take over before waiting for process shutdown so a
                # full ffmpeg pipe cannot turn a normal terminate into the
                # two-second timeout path.  Do not start this in the live
                # stream itself: one pipe must always have exactly one reader.
                stdout_drain = FfmpegStdoutDrain(self.process, name=self.context).start()
                if self.stop_process is None:
                    succeeded = await stop_camera_process(self.process, self.context)
                else:
                    succeeded = await self.stop_process(self.process)
        except Exception as exc:
            succeeded = False
            logger.error("Camera process cleanup failed [%s]: %s", self.context, summarize_ffmpeg_stderr(str(exc)))
        finally:
            if self.process is not None and self.process.returncode is None:
                _unreaped_processes[self.process.pid] = self.process
            try:
                if stdout_drain is not None:
                    await stdout_drain.aclose()
                # Keep draining until ffmpeg exits (or is explicitly handed off).
                if self.stderr is not None:
                    await self.stderr.aclose()
            except Exception as exc:
                succeeded = False
                logger.error("Camera stderr cleanup failed [%s]: %s", self.context, summarize_ffmpeg_stderr(str(exc)))
            finally:
                if self.proxy is not None:
                    try:
                        await close_tls_proxy(self.proxy)
                    except TimeoutError:
                        succeeded = False  # proxy coordinator already logged it
        if not succeeded:
            raise CameraCleanupError("Camera attempt cleanup failed")
