"""The capture service's resource limits and error taxonomy — spec §6.

Two capture workers per process, 1 MiB chunks, a 60-second no-progress deadline,
a 30-minute ceiling and a 256 MiB free-space reserve that counts what the other
workers have reserved but not yet written. None of that is waited out in real
time: the clock is a fake and a hung read is a ``threading.Event``, so the test
that exercises the 30-minute ceiling still runs in milliseconds.

The last test asserts the loop and the shared executor are intact. The workers
are daemon threads that deliberately outlive a timeout (§6: an overdue syscall
keeps its slot until it really returns), so a test that forgot to release one
would leak it into the next — and a leaked thread is a defect, not noise.
"""

from __future__ import annotations

import asyncio
import errno
import threading
import time
import zipfile
from pathlib import Path

import pytest

from backend.app.models.queue_source import FORMAT_3MF, FORMAT_GCODE
from backend.app.services import queue_sources

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def make_3mf(path: Path, *, payload: bytes = b"<model/>") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", payload)
    return path.read_bytes()


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Gate:
    """A source handle that blocks inside ``read()`` — the hung SMB read of §6."""

    def __init__(self, release: threading.Event, *, data: bytes = b"") -> None:
        self.release = release
        self.data = data
        self.reading = threading.Event()
        self.reads: list[int] = []

    def __enter__(self) -> Gate:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.reads.append(size)
        self.reading.set()
        self.release.wait(30)
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class Bench:
    """Hands each worker its own gated handle and re-opens every gate on the way out."""

    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.events: list[threading.Event] = []

    def arm(self, count: int = 1, *, data: bytes = b"") -> list[Gate]:
        gates = []
        for _ in range(count):
            release = threading.Event()
            self.events.append(release)
            gates.append(Gate(release, data=data))
        queue = list(gates)
        self.monkeypatch.setattr(queue_sources, "_open_source", lambda path: queue.pop(0))
        return gates

    def drain(self) -> None:
        for event in self.events:
            event.set()
        deadline = time.monotonic() + 10
        while queue_sources.active_captures() and time.monotonic() < deadline:
            time.sleep(0.01)


@pytest.fixture(autouse=True)
def bench(monkeypatch):
    queue_sources._reset_state()
    harness = Bench(monkeypatch)
    yield harness
    harness.drain()
    queue_sources._reset_state()


@pytest.fixture
def fast_poll(monkeypatch):
    """Watch the fake clock every 10 ms instead of every half second."""
    monkeypatch.setattr(queue_sources, "PROGRESS_POLL_SECONDS", 0.01)


def request_for(path: Path, fmt: str = FORMAT_3MF, **kwargs) -> queue_sources.CaptureRequest:
    return queue_sources.CaptureRequest(path=path, format=fmt, display_filename=path.name, **kwargs)


def staged_parts() -> list[Path]:
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.exists() else []


async def wait_for(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


def capture_task(source: Path, **kwargs) -> asyncio.Task:
    return asyncio.create_task(queue_sources.capture(request_for(source, **kwargs)))


async def cancel_all(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------------- #
# The numbers themselves
# --------------------------------------------------------------------------- #


def test_the_limits_are_the_numbers_the_spec_agreed():
    assert queue_sources.CAPTURE_WORKERS == 2
    assert queue_sources.HYDRATION_WORKERS == 1
    assert queue_sources.CHUNK_SIZE == 1024 * 1024
    assert queue_sources.NO_PROGRESS_SECONDS == 60.0
    assert queue_sources.CAPTURE_CEILING_SECONDS == 30 * 60.0
    assert queue_sources.FREE_SPACE_RESERVE_BYTES == 256 * 1024 * 1024
    assert queue_sources.GC_MIN_INTERVAL_SECONDS == 15 * 60.0
    assert queue_sources.ORPHAN_GRACE_SECONDS == 60 * 60.0


def test_the_error_taxonomy_is_the_one_the_api_will_map():
    expected = {
        queue_sources.QueueSourceBusy: (503, "source_copy_busy"),
        queue_sources.StorageReplaced: (503, "source_spool_replaced"),
        queue_sources.SourceUnreadable: (422, "source_unreadable"),
        queue_sources.SourceChanged: (422, "source_changed"),
        queue_sources.SourceInvalid: (422, "source_invalid"),
        queue_sources.CaptureTimeout: (504, "source_copy_timeout"),
        queue_sources.NoSpace: (507, "source_spool_no_space"),
        queue_sources.WriteFailed: (507, "source_spool_write_failed"),
    }
    for error, (status, reason) in expected.items():
        assert issubclass(error, queue_sources.QueueSourceError)
        instance = error()
        assert (instance.http_status, instance.reason) == (status, reason)
    # A replaced spool is a retry, so it must answer 503 wherever busy does.
    assert issubclass(queue_sources.StorageReplaced, queue_sources.QueueSourceBusy)


def test_the_spool_root_is_read_from_the_settings_at_call_time(tmp_path, monkeypatch):
    from backend.app.core.config import settings

    assert queue_sources.spool_root() == Path(settings.data_dir) / "queue-sources"
    monkeypatch.setattr(settings, "data_dir", tmp_path / "moved", raising=False)
    assert queue_sources.spool_root() == tmp_path / "moved" / "queue-sources"
    sha = "a" * 64
    assert queue_sources.object_relative_path(sha, FORMAT_3MF) == f"queue-sources/objects/aa/{sha}.3mf"
    with pytest.raises(ValueError):
        queue_sources.object_path("not-a-hash", FORMAT_3MF)


# --------------------------------------------------------------------------- #
# Admission — two workers, no waiting queue
# --------------------------------------------------------------------------- #


async def test_a_third_admission_while_two_workers_are_busy_is_refused_without_a_thread(tmp_path, bench):
    source = tmp_path / "share" / "a.3mf"
    make_3mf(source)
    bench.arm(2)
    first, second = capture_task(source), capture_task(source)
    await wait_for(lambda: len(staged_parts()) == 2)
    assert queue_sources.active_captures() == 2
    before = [t for t in threading.enumerate() if t.name.startswith(queue_sources.THREAD_NAME_PREFIX)]

    with pytest.raises(queue_sources.QueueSourceBusy, match="source_copy_busy"):
        await queue_sources.capture(request_for(source))

    after = [t for t in threading.enumerate() if t.name.startswith(queue_sources.THREAD_NAME_PREFIX)]
    assert len(after) == len(before) == 2
    assert len(staged_parts()) == 2
    assert queue_sources.active_captures() == 2
    await cancel_all(first, second)


async def test_a_refusal_does_not_consume_a_slot(tmp_path, bench):
    source = tmp_path / "share" / "a.3mf"
    raw = make_3mf(source)
    bench.arm(3, data=raw)
    first, second = capture_task(source), capture_task(source)
    await wait_for(lambda: queue_sources.active_captures() == 2)
    with pytest.raises(queue_sources.QueueSourceBusy):
        await queue_sources.capture(request_for(source))

    bench.drain()
    receipts = await asyncio.gather(first, second)
    assert {r.size_bytes for r in receipts} == {len(raw)}
    await wait_for(lambda: queue_sources.active_captures() == 0)

    third = await queue_sources.capture(request_for(source))
    assert third.size_bytes == len(raw)


async def test_background_hydration_takes_at_most_one_slot(tmp_path, bench):
    source = tmp_path / "share" / "a.3mf"
    make_3mf(source)
    bench.arm(2)
    hydration = capture_task(source, background=True)
    await wait_for(lambda: queue_sources.active_captures() == 1)

    with pytest.raises(queue_sources.QueueSourceBusy):
        await queue_sources.capture(request_for(source, background=True))

    # The operator's own add still gets the second slot.
    foreground = capture_task(source)
    await wait_for(lambda: queue_sources.active_captures() == 2)
    await cancel_all(hydration, foreground)


# --------------------------------------------------------------------------- #
# Free space
# --------------------------------------------------------------------------- #


async def test_a_capture_that_would_eat_the_reserve_is_refused(tmp_path, monkeypatch):
    source = tmp_path / "share" / "a.3mf"
    raw = make_3mf(source)
    assert len(raw) > 10
    monkeypatch.setattr(queue_sources, "_disk_free", lambda path: queue_sources.FREE_SPACE_RESERVE_BYTES + 10)

    with pytest.raises(queue_sources.NoSpace, match="source_spool_no_space"):
        await queue_sources.capture(request_for(source))

    assert staged_parts() == []
    assert queue_sources.active_captures() == 0


async def test_the_free_space_check_counts_another_workers_unwritten_reservation(tmp_path, monkeypatch, bench):
    source = tmp_path / "share" / "a.3mf"
    size = len(make_3mf(source))
    # Room for one copy plus the reserve, not for two.
    monkeypatch.setattr(
        queue_sources, "_disk_free", lambda path: queue_sources.FREE_SPACE_RESERVE_BYTES + size + size // 2
    )
    bench.arm(1)
    first = capture_task(source)
    await wait_for(lambda: len(staged_parts()) == 1)  # reserved, nothing written yet

    with pytest.raises(queue_sources.NoSpace):
        await queue_sources.capture(request_for(source))

    await cancel_all(first)


async def test_enospc_and_a_write_failure_map_to_the_two_507_codes(tmp_path, monkeypatch):
    source = tmp_path / "share" / "a.3mf"
    make_3mf(source)

    def no_space(path):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(queue_sources, "_open_staging", no_space)
    with pytest.raises(queue_sources.NoSpace):
        await queue_sources.capture(request_for(source))

    def denied(path):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(queue_sources, "_open_staging", denied)
    with pytest.raises(queue_sources.WriteFailed, match="source_spool_write_failed"):
        await queue_sources.capture(request_for(source))
    assert queue_sources.active_captures() == 0


# --------------------------------------------------------------------------- #
# The source side
# --------------------------------------------------------------------------- #


async def test_an_unreadable_source_is_refused(tmp_path):
    with pytest.raises(queue_sources.SourceUnreadable, match="source_unreadable"):
        await queue_sources.capture(request_for(tmp_path / "share" / "gone.3mf"))

    folder = tmp_path / "share" / "a-directory.3mf"
    folder.mkdir(parents=True)
    with pytest.raises(queue_sources.SourceUnreadable):
        await queue_sources.capture(request_for(folder))
    assert queue_sources.active_captures() == 0


async def test_a_read_that_fails_midway_is_a_source_failure(tmp_path, monkeypatch):
    source = tmp_path / "share" / "a.3mf"
    make_3mf(source)

    class Exploding:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self, size):
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(queue_sources, "_open_source", lambda path: Exploding())
    with pytest.raises(queue_sources.SourceUnreadable):
        await queue_sources.capture(request_for(source))
    assert staged_parts() == []


async def test_the_worker_reads_the_configured_chunk_size(tmp_path, monkeypatch):
    source = tmp_path / "share" / "a.gcode"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"G1 X1\n" * 100)
    monkeypatch.setattr(queue_sources, "CHUNK_SIZE", 64)
    sizes: list[int] = []
    real_open = queue_sources._open_source

    class Recorder:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self.handle.__exit__(*exc)

        def read(self, size):
            sizes.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(queue_sources, "_open_source", lambda path: Recorder(real_open(path)))

    receipt = await queue_sources.capture(request_for(source, FORMAT_GCODE))

    assert receipt.size_bytes == 600
    assert set(sizes) == {64}
    assert len(sizes) == 600 // 64 + 2  # nine full chunks, a short one, then EOF


# --------------------------------------------------------------------------- #
# Deadlines — S6: a late worker owns its own staging and publishes nothing
# --------------------------------------------------------------------------- #


async def test_a_stalled_read_times_out_and_the_worker_keeps_its_own_staging(tmp_path, monkeypatch, bench, fast_poll):
    clock = Clock()
    monkeypatch.setattr(queue_sources, "_now", clock)
    source = tmp_path / "share" / "a.3mf"
    raw = make_3mf(source)
    # The gate holds the WHOLE file: once released this worker would finish a
    # perfectly good copy. S6 says it must still publish nothing.
    gate = bench.arm(1, data=raw)[0]
    task = capture_task(source)
    await wait_for(lambda: gate.reading.is_set())
    part = staged_parts()
    assert len(part) == 1

    clock.advance(queue_sources.NO_PROGRESS_SECONDS + 1)
    with pytest.raises(queue_sources.CaptureTimeout, match="source_copy_timeout"):
        await task

    # The slot and the .part belong to the live worker until it really returns.
    assert queue_sources.active_captures() == 1
    assert staged_parts() == part
    assert queue_sources.pinned_staging_paths() == frozenset()
    bench.drain()
    await wait_for(lambda: queue_sources.active_captures() == 0)
    # The worker cleaned up after itself — nobody else ever touched its file,
    # and its late success was not handed to anybody.
    assert staged_parts() == []
    assert queue_sources.pinned_staging_paths() == frozenset()


async def test_a_worker_that_finishes_after_the_timeout_hands_nothing_over(tmp_path, monkeypatch, bench, fast_poll):
    """S6, the race the loop's own stop check cannot cover.

    Here the copy is already complete and correct when the supervisor gives up —
    the worker is inside the container validation, past every cancellation
    check. Its late success must still reach nobody, and its bytes must go.
    """
    clock = Clock()
    monkeypatch.setattr(queue_sources, "_now", clock)
    source = tmp_path / "share" / "a.3mf"
    make_3mf(source)
    release = threading.Event()
    bench.events.append(release)
    validating = threading.Event()

    def slow_validation(part, fmt):
        validating.set()
        release.wait(30)

    monkeypatch.setattr(queue_sources, "_validate_container", slow_validation)
    task = capture_task(source)
    await wait_for(validating.is_set)
    part = staged_parts()
    assert len(part) == 1

    clock.advance(queue_sources.NO_PROGRESS_SECONDS + 1)
    with pytest.raises(queue_sources.CaptureTimeout):
        await task
    assert queue_sources.active_captures() == 1

    release.set()
    await wait_for(lambda: queue_sources.active_captures() == 0)
    assert staged_parts() == []
    assert queue_sources.pinned_staging_paths() == frozenset()


async def test_the_overall_ceiling_stops_a_capture_that_is_still_making_progress(tmp_path, monkeypatch, fast_poll):
    clock = Clock()
    monkeypatch.setattr(queue_sources, "_now", clock)
    source = tmp_path / "share" / "a.3mf"
    raw = make_3mf(source)

    class Slow:
        """Every chunk arrives — each one half a minute after the last."""

        def __init__(self) -> None:
            self.data = raw * 1000

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self, size):
            clock.advance(30.0)
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

    monkeypatch.setattr(queue_sources, "CHUNK_SIZE", 16)
    monkeypatch.setattr(queue_sources, "_open_source", lambda path: Slow())

    with pytest.raises(queue_sources.CaptureTimeout):
        await queue_sources.capture(request_for(source))

    await wait_for(lambda: queue_sources.active_captures() == 0)
    assert staged_parts() == []


async def test_a_cancelled_capture_publishes_nothing_and_owns_its_staging(tmp_path, bench):
    source = tmp_path / "share" / "a.3mf"
    raw = make_3mf(source)
    # Again the full file, so the cancelled worker would otherwise succeed late.
    gate = bench.arm(1, data=raw)[0]
    task = capture_task(source)
    await wait_for(lambda: gate.reading.is_set())
    part = staged_parts()

    await cancel_all(task)

    assert queue_sources.active_captures() == 1
    assert staged_parts() == part
    bench.drain()
    await wait_for(lambda: queue_sources.active_captures() == 0)
    assert staged_parts() == []
    assert queue_sources.pinned_staging_paths() == frozenset()


# --------------------------------------------------------------------------- #
# Pins
# --------------------------------------------------------------------------- #


async def test_pins_are_counted_and_released():
    assert queue_sources.pinned_source_ids() == frozenset()
    async with queue_sources.pin(7):
        assert queue_sources.pinned_source_ids() == frozenset({7})
        async with queue_sources.pin(7):
            assert queue_sources.pinned_source_ids() == frozenset({7})
        assert queue_sources.pinned_source_ids() == frozenset({7})
    assert queue_sources.pinned_source_ids() == frozenset()

    assert queue_sources.backup_pinned() is False
    async with queue_sources.pin_backup():
        assert queue_sources.backup_pinned() is True
    assert queue_sources.backup_pinned() is False


async def test_a_pin_survives_an_exception_in_its_body():
    with pytest.raises(RuntimeError):
        async with queue_sources.pin(3):
            raise RuntimeError("boom")
    assert queue_sources.pinned_source_ids() == frozenset()


async def test_resetting_the_module_under_a_live_worker_is_refused(tmp_path, bench):
    """A leaked worker must fail a test loudly, not be reset out from under it."""
    source = tmp_path / "share" / "a.3mf"
    make_3mf(source)
    gate = bench.arm(1)[0]
    task = capture_task(source)
    await wait_for(lambda: gate.reading.is_set())

    with pytest.raises(RuntimeError, match="still alive"):
        queue_sources._reset_state()

    await cancel_all(task)
    bench.drain()
    await wait_for(lambda: queue_sources.active_captures() == 0)
    queue_sources._reset_state()  # and now it is allowed


# --------------------------------------------------------------------------- #
# The module must leave nothing behind
# --------------------------------------------------------------------------- #


async def test_the_loop_and_the_default_executor_are_still_healthy():
    assert await asyncio.to_thread(lambda: "shared executor alive") == "shared executor alive"
    assert asyncio.get_running_loop().is_running()
    assert queue_sources.active_captures() == 0
    leaked = [t.name for t in threading.enumerate() if t.name.startswith(queue_sources.THREAD_NAME_PREFIX)]
    assert leaked == []
