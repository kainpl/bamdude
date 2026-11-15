"""One actual operation per printer file endpoint, owned until it really ends.

G10 unit A (spec r4 §3.3, §5.1, U0 acceptance §3.1). A printer's file services
take about one session at a time, and what has to be serialised is the
**actual** session — the socket a worker holds — not the coroutine that asked
for it. ``asyncio.wait_for`` and a cancelled caller stop the *waiting*; they do
not stop a thread already inside ``recv``. A lock released on the coroutine's
exit therefore let a second session open beside a live one (U0 probes R1/R2), and
a download queued on the shared executor connected and wrote its file after its
caller had been cancelled and the lock was free (Q1).

So here:

* **An FTP operation runs on a thread the lane starts and owns**, never on the
  shared default executor. The worker is registered with the lane and the
  runtime **before** ``Thread.start``; a failed start has its own terminal
  path. ``Thread.start`` returning proves nothing about the target running, so
  the worker checks its cancel intent and the runtime's admission itself before
  the first connect.
* **The lane is released only when every worker of the hold has ended** — on
  the worker's own completion, delivered to the loop. A holder that leaves
  early (timeout, cancel) leaves the lane locked; the last worker unlocks it.
* **Attempts inside one hold never overlap**: a new worker waits until the
  previous one has really ended.
* **Waiting never costs a thread**: the lane and the global worker bound are
  asyncio waits on the loop, with the caller's admission deadline. The global
  bound is what the shared executor allowed before (``min(32, cpus + 4)``), so
  owning threads does not become an unbounded fan-out.
* **Cancel closes the operation's own sockets** (registered abort hooks) and
  the lane still waits for the thread to end.
* **The tunnel is native asyncio**: its operation runs as a runtime-owned task
  in the loop, and the lane is released by that task's own ``finally``.

Restore closes admission before its first await, asks every in-flight
operation to stop and proves they ended, or refuses (``quiesce`` → False).
Only lifespan startup reopens admission.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The bound the shared default executor gave FTP work before it got its own
#: threads (``ThreadPoolExecutor``'s default worker count).
GLOBAL_FTP_WORKERS = min(32, (os.cpu_count() or 1) + 4)


class LaneRefused(Exception):
    """The operation was never started: ``reason`` is ``restoring`` or ``busy``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class WorkerCancelled(Exception):
    """The worker saw its cancel intent before it touched the printer."""


class LaneTimeout(Exception):
    """The caller's wait ran out; the worker may still be running and still owns the lane."""


class LaneWorker:
    """One blocking FTP operation on a thread the lane owns."""

    def __init__(self, runtime: PrinterFileRuntime, lane: _Lane, name: str, target: Callable[[LaneWorker], Any]):
        self.runtime = runtime
        self.lane = lane
        self.name = name
        self._target = target
        self.loop = lane.loop
        self.future: asyncio.Future = self.loop.create_future()
        # An awaited-nowhere exception must not become a "never retrieved" warning:
        # the caller may have left (cancel / timeout) and the outcome is still owned.
        self.future.add_done_callback(_retrieve)
        self.done = threading.Event()
        self.cancel_reason: str | None = None
        self._hooks: list[Callable[[], None]] = []
        self._hooks_lock = threading.Lock()
        # The global worker slot this worker holds; released by its own settle.
        self.slot: asyncio.Semaphore | None = None

    # -- seen from the worker thread --------------------------------------------------

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_reason is not None

    def checkpoint(self) -> None:
        """Raise :class:`WorkerCancelled` once the operation may no longer touch the printer."""
        if self.cancel_requested or not self.runtime.admitting:
            raise WorkerCancelled(self.cancel_reason or "restoring")

    def attach(self, abort: Callable[[], None]) -> None:
        """Register how to close this operation's own I/O; runs at once if cancel already came."""
        with self._hooks_lock:
            self._hooks.append(abort)
            late = self.cancel_requested
        if late:
            _quietly(abort)

    def detach(self, abort: Callable[[], None]) -> None:
        with self._hooks_lock:
            if abort in self._hooks:
                self._hooks.remove(abort)

    # -- seen from the loop -------------------------------------------------------------

    def request_cancel(self, reason: str) -> None:
        """Ask the operation to stop and close its sockets. The lane still waits for the thread."""
        with self._hooks_lock:
            if self.cancel_reason is None:
                self.cancel_reason = reason
            hooks = list(self._hooks)
        for hook in hooks:
            _quietly(hook)

    def _run(self) -> None:
        started = time.monotonic()
        outcome: tuple[Any, BaseException | None]
        try:
            self.checkpoint()
            outcome = (self._target(self), None)
        except BaseException as exc:  # noqa: BLE001 — delivered to the owner, never swallowed
            outcome = (None, exc)
        finally:
            self.done.set()
        self.runtime._thread_finished(self, outcome, time.monotonic() - started)


class _Lane:
    """Admission for one endpoint; locked from a hold until its last worker has ended."""

    def __init__(self, key: str, loop: asyncio.AbstractEventLoop):
        self.key = key
        self.loop = loop
        self.lock = asyncio.Lock()
        self.workers: set[LaneWorker] = set()
        self.tasks: set[asyncio.Task] = set()
        self.release_pending = False

    def busy(self) -> bool:
        return bool(self.workers or self.tasks)

    def maybe_release(self) -> None:
        if self.release_pending and not self.busy():
            self.release_pending = False
            if self.lock.locked():
                self.lock.release()


class PrinterFileRuntime:
    """Admission, lanes and the registry of every operation that still owns a printer session."""

    def __init__(self, *, global_workers: int = GLOBAL_FTP_WORKERS):
        self.closed = False
        self.global_workers = global_workers
        self._lanes: dict[str, _Lane] = {}
        self._slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._registry_lock = threading.Lock()
        self._workers: set[LaneWorker] = set()
        self._tasks: set[asyncio.Task] = set()

    # -- admission ----------------------------------------------------------------------

    @property
    def admitting(self) -> bool:
        return not self.closed

    def close_admission(self) -> None:
        self.closed = True

    def reopen(self) -> None:
        """Only lifespan startup reopens; a refused drain leaves nothing to reopen onto."""
        if self.in_flight():
            raise RuntimeError("printer file operations are still running")
        self.closed = False

    def in_flight(self) -> int:
        with self._registry_lock:
            return len(self._workers) + len(self._tasks)

    async def quiesce(self, seconds: float = 30.0) -> bool:
        """Close admission, stop every operation, prove they ended. False = an owner is still alive."""
        self.close_admission()
        with self._registry_lock:
            workers = list(self._workers)
            tasks = list(self._tasks)
        for worker in workers:
            worker.request_cancel("restoring")
        for task in tasks:
            task.cancel()
        pending = [w.future for w in workers] + tasks
        if pending:
            await asyncio.wait(pending, timeout=seconds)
        remaining = self.in_flight()
        if remaining:
            logger.warning("Printer file operations still own %d session(s) after %.0fs", remaining, seconds)
        return remaining == 0

    def _require_admitting(self) -> None:
        if self.closed:
            raise LaneRefused("restoring")

    # -- lanes ----------------------------------------------------------------------------

    def _lane(self, key: str) -> _Lane:
        loop = asyncio.get_running_loop()
        lane = self._lanes.get(key)
        if lane is None or lane.loop is not loop:
            lane = _Lane(key, loop)
            self._lanes[key] = lane
        return lane

    def _global(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._slots.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(self.global_workers)
            self._slots[loop] = sem
        return sem

    def lane_busy(self, key: str) -> bool:
        lane = self._lanes.get(key)
        return bool(lane and (lane.lock.locked() or lane.busy()))

    @asynccontextmanager
    async def hold(self, key: str, *, deadline: float | None = None):
        """Hold one endpoint. Leaving early never unlocks it under a live worker."""
        self._require_admitting()
        lane = self._lane(key)
        try:
            await _within(lane.lock.acquire(), deadline)
        except TimeoutError:
            raise LaneRefused("busy") from None
        if self.closed:
            lane.lock.release()
            raise LaneRefused("restoring")
        try:
            yield lane
        finally:
            if lane.busy():
                lane.release_pending = True
            else:
                lane.lock.release()

    # -- FTP workers ---------------------------------------------------------------------

    async def start(
        self, lane: _Lane, name: str, target: Callable[[LaneWorker], Any], *, deadline: float | None = None
    ) -> LaneWorker:
        """Start one owned worker on a held lane, after the previous one has really ended."""
        try:
            while lane.workers:
                await _within(asyncio.wait([w.future for w in lane.workers]), deadline)
            slot = self._global()
            await _within(slot.acquire(), deadline)
        except TimeoutError:
            raise LaneRefused("busy") from None
        if self.closed:
            slot.release()
            raise LaneRefused("restoring")
        worker = LaneWorker(self, lane, name, target)
        worker.slot = slot
        # Registered BEFORE Thread.start: from here the worker is owned even if
        # the OS defers the thread or the caller is cancelled.
        lane.workers.add(worker)
        with self._registry_lock:
            self._workers.add(worker)
        thread = threading.Thread(target=worker._run, name=f"printer-file-{name}", daemon=True)
        try:
            thread.start()
        except BaseException as exc:
            self._settle(worker, (None, exc))
            raise
        return worker

    async def wait(self, worker: LaneWorker, timeout: float | None = None) -> Any:
        """The worker's result. Timeout → :class:`LaneTimeout` (it keeps running); cancel asks it to stop."""
        try:
            return await _within(asyncio.shield(worker.future), _deadline(timeout))
        except TimeoutError:
            raise LaneTimeout(worker.name) from None
        except asyncio.CancelledError:
            worker.request_cancel("cancelled")
            raise

    async def call(
        self,
        key: str,
        name: str,
        target: Callable[[LaneWorker], T],
        *,
        admission_timeout: float | None = None,
        timeout: float | None = None,
    ) -> T:
        """Hold ``key``, run one owned worker, return its result.

        ``admission_timeout`` bounds the wait for the lane and a worker slot (the
        #2572 concern: a caller holding a DB connection must not wait for ever);
        ``timeout`` bounds the wait for the result. Neither ever frees the lane
        under a live thread.
        """
        deadline = _deadline(admission_timeout)
        async with self.hold(key, deadline=deadline) as lane:
            worker = await self.start(lane, name, target, deadline=deadline)
            try:
                return await self.wait(worker, timeout)
            except LaneTimeout:
                worker.request_cancel("timeout")
                raise

    def _thread_finished(self, worker: LaneWorker, outcome, elapsed: float) -> None:
        try:
            worker.loop.call_soon_threadsafe(self._settle, worker, outcome)
        except RuntimeError:
            # The loop is gone (interpreter or test teardown): nobody awaits it any more.
            with self._registry_lock:
                self._workers.discard(worker)
        if elapsed > 60:
            logger.info("Printer file operation %s ran %.0fs", worker.name, elapsed)

    def _settle(self, worker: LaneWorker, outcome) -> None:
        value, error = outcome
        if not worker.future.done():
            if error is not None:
                worker.future.set_exception(error)
            else:
                worker.future.set_result(value)
        lane = worker.lane
        if worker in lane.workers:
            lane.workers.discard(worker)
            if worker.slot is not None:
                worker.slot.release()
        with self._registry_lock:
            self._workers.discard(worker)
        lane.maybe_release()

    # -- tunnel tasks --------------------------------------------------------------------

    async def run_task(
        self, key: str, name: str, factory: Callable[[], Awaitable[T]], *, admission_timeout: float | None = None
    ) -> T:
        """Run one native-asyncio operation as a runtime-owned task on a held lane."""
        deadline = _deadline(admission_timeout)
        async with self.hold(key, deadline=deadline) as lane:
            task = asyncio.get_running_loop().create_task(factory(), name=f"printer-file-{name}")
            lane.tasks.add(task)
            with self._registry_lock:
                self._tasks.add(task)

            def _done(finished: asyncio.Task) -> None:
                lane.tasks.discard(finished)
                with self._registry_lock:
                    self._tasks.discard(finished)
                if not finished.cancelled():
                    finished.exception()
                lane.maybe_release()

            task.add_done_callback(_done)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
                raise


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else asyncio.get_running_loop().time() + timeout


async def _within(awaitable: Awaitable[T], deadline: float | None) -> T:
    if deadline is None:
        return await awaitable
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError
    async with asyncio.timeout(remaining):
        return await awaitable


def _retrieve(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


def _quietly(hook: Callable[[], None]) -> None:
    try:
        hook()
    except Exception:  # noqa: BLE001 — an abort is best-effort; the lane waits for the thread anyway
        logger.debug("Printer file abort hook failed", exc_info=True)


printer_file_runtime = PrinterFileRuntime()


def ftp_key(ip_address: str) -> str:
    return f"ftp:{ip_address}"


def tunnel_key(ip_address: str, port: int) -> str:
    return f"tunnel:{ip_address}:{port}"
