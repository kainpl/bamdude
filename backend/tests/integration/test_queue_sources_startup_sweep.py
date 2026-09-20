"""What the previous process left in the spool — spec §9's tail and §11.

Spec: ``60-specs/queue-source-spool-spec.md``. §9: "Після crash startup добирає
deleting/unreferenced artifacts; broken referenced файли не евікати". §11: "На
restart не видаляти referenced blobs, не реактивувати failed і не починати
повторний фізичний друк лише через готовий snapshot".

There are **three** kinds of leftovers, not two:

* a ``deleting`` tombstone — the GC was unlinking when the process died;
* a stray ``staging/*.part`` — a capture that never finished;
* a fully renamed **object with no row at all** — the publication moved the
  staged file and died before its commit. A cancellation inside the install
  leaves exactly that, by design (``test_queue_sources_capture.py``), and §5
  step 7 hands the unneeded bytes to cleanup.

The second half of this module is the wiring: the sweep and the periodic GC are
tracked background tasks that never block the lifespan and are cancelled at
shutdown.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import FORMAT_3MF, STATE_DELETING, QueueSource
from backend.app.services import queue_sources

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def make_3mf(path: Path, *, payload: bytes = b"<model/>") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", payload)
        zf.writestr("Metadata/plate_1.png", b"\x89PNG" + b"\x00" * 64)
    return path.read_bytes()


@pytest.fixture(autouse=True)
def clean_spool_state():
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def publish_blob(sessions, path: Path, payload: bytes = b"<model/>") -> QueueSource:
    make_3mf(path, payload=payload)
    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(path=path, format=FORMAT_3MF, display_filename=path.name)
    )

    async def attach(session: AsyncSession, source: QueueSource) -> None:
        return None

    return await queue_sources.publish(receipt, attach, session_factory=sessions)


def object_of(source: QueueSource) -> Path:
    return Path(settings.base_dir) / source.relative_path


def staged_parts() -> list[Path]:
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.exists() else []


def age_by(path: Path, seconds: float) -> None:
    moment = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).timestamp()
    os.utime(path, (moment, moment))


async def wait_for(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:  # pragma: no cover - a failed wait fails its own test
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- #
# The three leftovers
# --------------------------------------------------------------------------- #


async def test_the_sweep_finishes_a_tombstone_the_crash_interrupted(tmp_path, sessions):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    async with sessions() as session:
        row = await session.get(QueueSource, source.id)
        row.state = STATE_DELETING
        await session.commit()

    await queue_sources.sweep_after_restart(session_factory=sessions)

    assert not object_of(source).exists()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 0


async def test_the_sweep_drops_a_dead_writers_part_without_waiting_out_the_grace(tmp_path, sessions):
    """At boot no ``.part`` can have a live writer — this process holds no slots.

    So the one-hour grace would only keep a crash's litter on disk. The objects
    beside it keep the full grace, because those are verified bytes and a wrong
    unlink costs a print.
    """
    queue_sources.staging_root().mkdir(parents=True, exist_ok=True)
    fresh = queue_sources.staging_root() / "abcdef01.part"
    fresh.write_bytes(b"half a file, written seconds before the crash")
    assert queue_sources.active_captures() == 0

    await queue_sources.sweep_after_restart(session_factory=sessions)

    assert not fresh.exists()
    assert staged_parts() == []


async def test_the_sweep_reaps_an_object_renamed_before_the_commit(tmp_path, sessions):
    """The third leftover: bytes in their final place with no row at all."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    orphan = object_of(source)
    async with sessions() as session:
        await session.delete(await session.get(QueueSource, source.id))
        await session.commit()
    age_by(orphan, queue_sources.ORPHAN_GRACE_SECONDS + 60)

    await queue_sources.sweep_after_restart(session_factory=sessions)

    assert not orphan.exists()


async def test_the_sweep_keeps_a_fresh_object_with_no_row_for_its_grace(tmp_path, sessions):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    orphan = object_of(source)
    async with sessions() as session:
        await session.delete(await session.get(QueueSource, source.id))
        await session.commit()

    await queue_sources.sweep_after_restart(session_factory=sessions)

    assert orphan.is_file()


async def test_the_sweep_never_touches_a_referenced_blob(tmp_path, sessions):
    """§11: a restart deletes no referenced blob and reactivates no failed job."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    async with sessions() as session:
        session.add(PrintQueueItem(queue_id=1, status="failed", queue_source_id=source.id))
        await session.commit()

    await queue_sources.sweep_after_restart(session_factory=sessions)

    assert object_of(source).is_file()
    async with sessions() as session:
        assert (await session.get(QueueSource, source.id)) is not None
        item = await session.scalar(select(PrintQueueItem))
        assert item.status == "failed"


async def test_the_sweep_leaves_a_hydration_captures_part_alone(tmp_path, sessions, monkeypatch):
    """The sweep is a background task, so a capture can already be running.

    It waives the grace for ``.part`` files on the argument that no writer can be
    alive at boot — which stops being true the moment background hydration (§8)
    takes its slot, so the live-worker check has to hold anyway.

    Asserts the unlink was never **attempted**: Windows refuses to remove a file
    the worker still has open, so "the file is still there" would pass here for a
    platform reason that does not hold on Linux.
    """
    source = tmp_path / "share" / "lamp.3mf"
    raw = make_3mf(source)
    release, reading = threading.Event(), threading.Event()
    attempted: list[Path] = []
    monkeypatch.setattr(queue_sources, "_unlink_part", attempted.append)

    class Gate:
        def __init__(self) -> None:
            self.data = raw

        def __enter__(self) -> Gate:
            return self

        def __exit__(self, *exc) -> None:
            return None

        def read(self, size: int) -> bytes:
            reading.set()
            release.wait(30)
            chunk, self.data = self.data[:size], self.data[size:]
            return chunk

    monkeypatch.setattr(queue_sources, "_open_source", lambda path: Gate())
    task = asyncio.create_task(
        queue_sources.capture(
            queue_sources.CaptureRequest(path=source, format=FORMAT_3MF, display_filename="lamp.3mf", background=True)
        )
    )
    await wait_for(reading.is_set)
    part = staged_parts()
    assert len(part) == 1

    await queue_sources.sweep_after_restart(session_factory=sessions)

    assert attempted == []
    assert staged_parts() == part
    release.set()
    receipt = await task
    await receipt.discard()


# --------------------------------------------------------------------------- #
# The wiring — tracked tasks, no lifespan blocking, cancelled at shutdown
# --------------------------------------------------------------------------- #


async def test_the_sweep_does_not_block_the_lifespan(monkeypatch):
    from backend.app import main
    from backend.app.core import tasks as task_registry

    started, release = asyncio.Event(), asyncio.Event()

    async def blocking_sweep(**kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(queue_sources, "sweep_after_restart", blocking_sweep)
    monkeypatch.setattr(queue_sources, "GC_MIN_INTERVAL_SECONDS", 3600.0)
    before = task_registry.active_task_count()

    main.start_queue_source_collector()  # must return immediately

    await asyncio.wait_for(started.wait(), timeout=5)
    assert task_registry.active_task_count() == before + 2  # the sweep and the loop
    release.set()
    await main.stop_queue_source_collector()


async def test_the_periodic_gc_sleeps_before_its_first_pass_and_stops_on_shutdown(monkeypatch):
    from backend.app import main

    passes: list[bool] = []

    async def fake_collect(**kwargs):
        passes.append(True)
        return queue_sources.GcReport()

    async def fake_sweep(**kwargs):
        return None

    monkeypatch.setattr(queue_sources, "collect", fake_collect)
    monkeypatch.setattr(queue_sources, "sweep_after_restart", fake_sweep)
    monkeypatch.setattr(queue_sources, "GC_MIN_INTERVAL_SECONDS", 3600.0)

    main.start_queue_source_collector()
    for _ in range(20):
        await asyncio.sleep(0.005)
    # The first thing the loop does is wait: a pass at boot would race the sweep.
    assert passes == []

    await main.stop_queue_source_collector()

    monkeypatch.setattr(queue_sources, "GC_MIN_INTERVAL_SECONDS", 0.01)
    main.start_queue_source_collector()
    await wait_for(lambda: len(passes) >= 2)
    await main.stop_queue_source_collector()
    settled = len(passes)
    for _ in range(20):
        await asyncio.sleep(0.005)
    assert len(passes) == settled  # cancelled, not merely forgotten


async def test_starting_the_collector_twice_does_not_double_the_loop(monkeypatch):
    from backend.app import main
    from backend.app.core import tasks as task_registry

    async def fake_sweep(**kwargs):
        return None

    monkeypatch.setattr(queue_sources, "sweep_after_restart", fake_sweep)
    monkeypatch.setattr(queue_sources, "GC_MIN_INTERVAL_SECONDS", 3600.0)
    before = task_registry.active_task_count()

    main.start_queue_source_collector()
    main.start_queue_source_collector()
    await asyncio.sleep(0.01)

    assert task_registry.active_task_count() <= before + 2
    await main.stop_queue_source_collector()


async def test_a_failing_pass_does_not_kill_the_loop(monkeypatch):
    from backend.app import main

    calls: list[int] = []

    async def exploding_collect(**kwargs):
        calls.append(len(calls))
        raise RuntimeError("the volume went away")

    async def fake_sweep(**kwargs):
        return None

    monkeypatch.setattr(queue_sources, "collect", exploding_collect)
    monkeypatch.setattr(queue_sources, "sweep_after_restart", fake_sweep)
    monkeypatch.setattr(queue_sources, "GC_MIN_INTERVAL_SECONDS", 0.01)

    main.start_queue_source_collector()
    await wait_for(lambda: len(calls) >= 2)
    await main.stop_queue_source_collector()
