"""The printer file lane: one actual operation per endpoint, owned until it really ends (G10 A, M02/M06).

Every test drives the runtime with plain targets and threading events, so the
thread really is the owner and the assertions are about the lane, not timing.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from backend.app.services import printer_file_lanes as lanes
from backend.app.services.printer_file_lanes import (
    LaneRefused,
    LaneTimeout,
    LaneWorker,
    PrinterFileRuntime,
    WorkerCancelled,
)

KEY = "ftp:10.0.0.1"


class Session:
    """Counts concurrent 'sessions' a target holds."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.connects = 0

    def __enter__(self):
        with self.lock:
            self.active += 1
            self.connects += 1
            self.peak = max(self.peak, self.active)
        return self

    def __exit__(self, *exc):
        with self.lock:
            self.active -= 1


def gated(session: Session, gate: threading.Event, started: threading.Event | None = None, result="done"):
    def target(worker: LaneWorker):
        with session:
            if started is not None:
                started.set()
            gate.wait(10)
            return result

    return target


async def _until(predicate, seconds=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_the_lane_locked_until_the_thread_ends():
    runtime = PrinterFileRuntime()
    session, gate, started = Session(), threading.Event(), threading.Event()
    first = asyncio.create_task(runtime.call(KEY, "first", gated(session, gate, started)))
    assert await asyncio.to_thread(started.wait, 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # The caller is gone; its thread still holds the session, so the lane must stay locked.
    assert runtime.lane_busy(KEY)
    second = asyncio.create_task(runtime.call(KEY, "second", gated(session, threading.Event(), result="second")))
    await asyncio.sleep(0.2)
    assert not second.done()
    assert session.peak == 1
    gate.set()
    # The second operation's own gate is never set; give it one to finish.
    await _until(lambda: session.connects == 2)
    for worker in list(runtime._workers):
        worker.request_cancel("test")
    second.cancel()
    await asyncio.gather(second, return_exceptions=True)
    assert session.peak == 1


@pytest.mark.asyncio
async def test_a_worker_whose_thread_has_not_run_yet_never_connects_after_its_cancel(monkeypatch):
    """Thread.start returning proves nothing (U0 acceptance §3.1): the cancel intent is checked first."""
    runtime = PrinterFileRuntime()
    session = Session()
    deferred = threading.Event()
    original_run = LaneWorker._run

    def deferred_run(self):
        deferred.wait(10)  # the OS has not scheduled the target yet
        original_run(self)

    monkeypatch.setattr(LaneWorker, "_run", deferred_run)
    caller = asyncio.create_task(runtime.call(KEY, "deferred", gated(session, threading.Event())))
    await _until(lambda: runtime.in_flight() == 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert runtime.lane_busy(KEY)  # owned although its target never ran
    deferred.set()
    await _until(lambda: runtime.in_flight() == 0)
    await _until(lambda: not runtime.lane_busy(KEY))
    assert session.connects == 0


@pytest.mark.asyncio
async def test_a_failed_thread_start_releases_everything_it_took(monkeypatch):
    runtime = PrinterFileRuntime(global_workers=1)

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError):
        await runtime.call(KEY, "nope", lambda worker: "never")
    monkeypatch.undo()
    assert runtime.in_flight() == 0
    assert not runtime.lane_busy(KEY)
    # The one global slot came back: another operation can run.
    assert await runtime.call(KEY, "next", lambda worker: "ok") == "ok"


@pytest.mark.asyncio
async def test_repeated_cancellation_never_detaches_the_worker():
    runtime = PrinterFileRuntime()
    session, gate, started = Session(), threading.Event(), threading.Event()
    caller = asyncio.create_task(runtime.call(KEY, "busy", gated(session, gate, started)))
    assert await asyncio.to_thread(started.wait, 5)
    for _ in range(5):
        caller.cancel()
        await asyncio.sleep(0)
    await asyncio.gather(caller, return_exceptions=True)
    assert runtime.in_flight() == 1
    assert runtime.lane_busy(KEY)
    gate.set()
    await _until(lambda: runtime.in_flight() == 0)
    await _until(lambda: not runtime.lane_busy(KEY))


@pytest.mark.asyncio
async def test_a_timed_out_wait_asks_the_worker_to_stop_and_keeps_the_lane():
    runtime = PrinterFileRuntime()
    gate = threading.Event()
    aborted = threading.Event()

    def target(worker: LaneWorker):
        worker.attach(aborted.set)
        gate.wait(10)
        worker.checkpoint()
        return "late"

    with pytest.raises(LaneTimeout):
        await runtime.call(KEY, "slow", target, timeout=0.1)
    assert aborted.wait(5)  # its own I/O was told to close
    assert runtime.lane_busy(KEY)
    gate.set()
    await _until(lambda: not runtime.lane_busy(KEY))


@pytest.mark.asyncio
async def test_attempts_inside_one_hold_never_overlap():
    runtime = PrinterFileRuntime()
    session, gate, started = Session(), threading.Event(), threading.Event()
    async with runtime.hold(KEY) as lane:
        first = await runtime.start(lane, "attempt-1", gated(session, gate, started))
        assert await asyncio.to_thread(started.wait, 5)
        first.request_cancel("retry")
        own_gate = threading.Event()

        def abortable(worker: LaneWorker):
            with session:
                worker.attach(own_gate.set)
                own_gate.wait(10)
                worker.checkpoint()
                return "second"

        second_started = asyncio.create_task(runtime.start(lane, "attempt-2", abortable))
        await asyncio.sleep(0.2)
        assert not second_started.done()  # waits for attempt 1 to really end
        gate.set()
        second = await second_started
        second.request_cancel("test")
        with pytest.raises(WorkerCancelled):
            await runtime.wait(second, 5)
    assert session.peak == 1


@pytest.mark.asyncio
async def test_the_global_worker_bound_holds_across_endpoints():
    runtime = PrinterFileRuntime(global_workers=2)
    session, gate = Session(), threading.Event()
    calls = [asyncio.create_task(runtime.call(f"ftp:10.0.0.{n}", "op", gated(session, gate))) for n in range(1, 5)]
    await _until(lambda: session.active == 2)
    await asyncio.sleep(0.2)
    assert session.peak == 2
    gate.set()
    assert await asyncio.gather(*calls) == ["done"] * 4
    assert session.peak == 2


@pytest.mark.asyncio
async def test_an_admission_wait_is_bounded_and_starts_nothing():
    runtime = PrinterFileRuntime()
    session, gate, started = Session(), threading.Event(), threading.Event()
    first = asyncio.create_task(runtime.call(KEY, "holder", gated(session, gate, started)))
    assert await asyncio.to_thread(started.wait, 5)
    with pytest.raises(LaneRefused) as refused:
        await runtime.call(KEY, "waiter", gated(session, gate), admission_timeout=0.1)
    assert refused.value.reason == "busy"
    assert session.connects == 1
    gate.set()
    assert await first == "done"


@pytest.mark.asyncio
async def test_quiesce_stops_everything_and_proves_it_ended():
    runtime = PrinterFileRuntime()
    gate = threading.Event()

    def target(worker: LaneWorker):
        worker.attach(gate.set)  # the abort hook ends the "transfer"
        gate.wait(10)
        worker.checkpoint()
        return "finished"

    caller = asyncio.create_task(runtime.call(KEY, "transfer", target))
    await _until(lambda: runtime.in_flight() == 1)
    assert await runtime.quiesce(5) is True
    with pytest.raises(WorkerCancelled):
        await caller
    with pytest.raises(LaneRefused) as refused:
        await runtime.call(KEY, "after", lambda worker: "no")
    assert refused.value.reason == "restoring"
    runtime.reopen()
    assert await runtime.call(KEY, "reopened", lambda worker: "yes") == "yes"


@pytest.mark.asyncio
async def test_an_unproven_drain_refuses_and_keeps_admission_closed():
    runtime = PrinterFileRuntime()
    stuck, started = threading.Event(), threading.Event()

    def target(worker: LaneWorker):
        started.set()
        stuck.wait(10)  # ignores its abort: a socket that never returns
        return "eventually"

    caller = asyncio.create_task(runtime.call(KEY, "stuck", target))
    assert await asyncio.to_thread(started.wait, 5)
    assert await runtime.quiesce(0.2) is False
    with pytest.raises(RuntimeError):
        runtime.reopen()
    stuck.set()
    await asyncio.gather(caller, return_exceptions=True)
    await _until(lambda: runtime.in_flight() == 0)
    runtime.reopen()


@pytest.mark.asyncio
async def test_a_tunnel_task_owns_its_lane_until_it_has_closed():
    runtime = PrinterFileRuntime()
    closed = asyncio.Event()
    entered = asyncio.Event()

    async def operation():
        try:
            entered.set()
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0.05)  # closing the socket takes a turn of the loop
            closed.set()

    caller = asyncio.create_task(runtime.run_task("tunnel:10.0.0.1:6000", "list", operation))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await _until(lambda: closed.is_set())
    await _until(lambda: not runtime.lane_busy("tunnel:10.0.0.1:6000"))
    assert runtime.in_flight() == 0


def test_the_global_bound_is_what_the_shared_executor_allowed():
    import os

    assert min(32, (os.cpu_count() or 1) + 4) == lanes.GLOBAL_FTP_WORKERS


def test_restore_drains_printer_file_work_before_the_storage_guard_and_any_file_work():
    import inspect

    from backend.app.api.routes import settings as settings_routes

    source = inspect.getsource(settings_routes._restore_backup)
    gate = source.index("await printer_file_runtime.quiesce()")
    assert source.index("obico_detection_service.quiesce()") < gate
    assert gate < source.index("queue_sources.storage_mutation()") < source.index("files.prepare")
    assert gate < source.index("files.apply")
    assert "Printer file transfers have not stopped; retry restore after they finish" in source


def test_lifespan_reopens_at_startup_and_drains_before_printers_disconnect():
    import inspect

    from backend.app import main

    source = inspect.getsource(main._application_lifespan)
    reopen = source.index("printer_file_runtime.reopen()")
    shutdown = source.index("await printer_file_runtime.quiesce(10)")
    assert reopen < source.index("yield") < shutdown < source.index("printer_manager.disconnect_all()")
