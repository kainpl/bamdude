"""Releasing a queue source when its last owner is gone — spec §9.

Spec: ``60-specs/queue-source-spool-spec.md``. §9's owner table is the whole
subject: the owners of a blob are **every** job row of **both** queues that names
it, in **any** status, plus the short-lived writer / reader / backup pins. There
is no ``ref_count`` and no hidden TTL — the question is asked with a query, every
pass, under the one storage-mutation guard, and "count then unlink" without that
guard is forbidden (S5).

No test waits out a real grace window or a real 15-minute interval: the wall
clock is an argument (``collect(now=...)``), the throttle's clock is the module's
own ``_now`` seam, and a blocked unlink is a ``threading.Event``. The last test
asserts the loop and the shared executor survived, because a capture thread or a
file worker left behind is a defect and not noise.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import (
    FORMAT_3MF,
    STATE_BROKEN,
    STATE_DELETING,
    STATE_READY,
    QueueSource,
)
from backend.app.services import queue_sources

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def make_3mf(path: Path, *, payload: bytes = b"<model/>") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", payload)
        zf.writestr("Metadata/plate_1.png", b"\x89PNG" + b"\x00" * 64)
    return path.read_bytes()


class Clock:
    """The monotonic clock the throttle reads, moved by hand."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Clockface:
    """The wall clock, anchored to the real one.

    Anchored rather than a fixed date because the objects under test get their
    mtime from the filesystem while they are being published: a hardcoded 2026
    date would compare a fake ``now`` with a real mtime and the grace arithmetic
    would depend on when the suite is run.
    """

    def __init__(self) -> None:
        self.anchor = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    def at(self, offset: float = 0.0) -> datetime:
        return self.anchor + timedelta(seconds=offset)

    def past_grace(self, extra: float = 60.0) -> datetime:
        return self.at(queue_sources.ORPHAN_GRACE_SECONDS + extra)

    def epoch(self, offset: float = 0.0) -> float:
        return self.at(offset).replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture
def clockface() -> Clockface:
    return Clockface()


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
    """A real capture and a real publication — the object and its row together."""
    make_3mf(path, payload=payload)
    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(path=path, format=FORMAT_3MF, display_filename=path.name)
    )

    async def attach(session: AsyncSession, source: QueueSource) -> None:
        return None

    return await queue_sources.publish(receipt, attach, session_factory=sessions)


def object_of(source: QueueSource) -> Path:
    return Path(settings.base_dir) / source.relative_path


async def add_owner(sessions, source_id: int, *, model=PrintQueueItem, status: str = "pending") -> int:
    async with sessions() as session:
        if model is PrintQueueItem:
            # ``queue_id`` is the printer's own id by invariant; the GC never
            # joins through it, so the row carries only the columns the GC reads.
            row = PrintQueueItem(queue_id=1, status=status, queue_source_id=source_id)
        else:
            row = AutoQueueItem(status=status, queue_source_id=source_id)
        session.add(row)
        await session.commit()
        return row.id


async def rows_left(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(QueueSource))


async def state_of(sessions, source_id: int) -> str | None:
    async with sessions() as session:
        row = await session.get(QueueSource, source_id)
        return None if row is None else row.state


async def mark_of(sessions, source_id: int) -> datetime | None:
    async with sessions() as session:
        row = await session.get(QueueSource, source_id)
        return None if row is None else row.unreferenced_at


async def collect(sessions, moment: datetime, **kwargs) -> queue_sources.GcReport:
    kwargs.setdefault("force", True)
    return await queue_sources.collect(now=moment, session_factory=sessions, **kwargs)


def staged_parts() -> list[Path]:
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.exists() else []


def objects() -> list[Path]:
    root = queue_sources.objects_root()
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


async def test_startup_moves_the_pre_release_directory_and_repoints_rows(sessions, tmp_path):
    """An install that briefly used ``queue-spool`` keeps every queued job."""
    source = await publish_blob(sessions, tmp_path / "share" / "job.3mf")
    old_root = queue_sources.legacy_spool_root()
    new_root = queue_sources.spool_root()
    new_root.replace(old_root)

    async with sessions() as session:
        row = await session.get(QueueSource, source.id)
        assert row is not None
        row.relative_path = f"queue-spool/objects/{source.sha256[:2]}/{source.sha256}.3mf"
        await session.commit()

    assert await queue_sources.migrate_legacy_spool(session_factory=sessions)
    expected = queue_sources.object_relative_path(source.sha256, FORMAT_3MF)
    assert (Path(settings.data_dir) / expected).is_file()
    assert not old_root.exists()

    async with sessions() as session:
        row = await session.get(QueueSource, source.id)
        assert row is not None and row.relative_path == expected


def age(path: Path, moment: float) -> None:
    os.utime(path, (moment, moment))


async def wait_for(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:  # pragma: no cover - a failed wait fails its own test
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


def make_link(link: Path, target: Path) -> None:
    """A symlink, or a junction where Windows will not grant a symlink."""
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt" and target.is_dir():
        done = subprocess.run(  # noqa: S603 - fixed argv, test-only
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if done.returncode == 0:
            return
    pytest.skip("this platform will not let the test create a link inside the spool")


# --------------------------------------------------------------------------- #
# The owner table — §9. Any row of either queue, in any status.
# --------------------------------------------------------------------------- #


async def assert_owned_forever(sessions, source: QueueSource, clockface: Clockface) -> None:
    """Two passes, and neither may touch the blob or even start its grace.

    ⚠️ The mark matters as much as the release. A pass that failed to see the
    owner would only *stamp* ``unreferenced_at`` on its first run — so a single
    pass that asserts "still here" passes for an owner the GC cannot see at all.
    (Measured: dropping ``AutoQueueItem`` from the owner query left the one-pass
    version of this green.)
    """
    first = await collect(sessions, clockface.past_grace(extra=10 * 86400))
    assert (first.released, first.marked) == (0, 0)
    assert await mark_of(sessions, source.id) is None

    second = await collect(sessions, clockface.past_grace(extra=20 * 86400))
    assert second.released == 0
    assert await rows_left(sessions) == 1
    assert await state_of(sessions, source.id) == STATE_READY
    assert object_of(source).is_file()


@pytest.mark.parametrize("status", ["pending", "printing", "paused", "completed", "failed", "skipped", "cancelled"])
async def test_a_print_queue_row_in_any_status_keeps_its_blob(tmp_path, sessions, clockface, status):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await add_owner(sessions, source.id, status=status)

    await assert_owned_forever(sessions, source, clockface)


@pytest.mark.parametrize("status", ["pending", "assigned", "failed", "cancelled"])
async def test_an_auto_queue_row_in_any_status_keeps_its_blob(tmp_path, sessions, clockface, status):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await add_owner(sessions, source.id, model=AutoQueueItem, status=status)

    await assert_owned_forever(sessions, source, clockface)


async def test_a_printing_row_closed_on_age_still_owns_its_blob(tmp_path, sessions, clockface):
    """Spec §9's added row (2026-09-13): a stale closure is not a completion.

    ``main.py::_close_stale_printing_rows`` ends a row that has been ``printing``
    past its predicted end with no completion event and no plate answer. The
    operator has still not said what happened on that plate, so the blob stays:
    the reference is released by deletion, Clear plate or Repeat, never by the
    closure itself.
    """
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    owner_id = await add_owner(sessions, source.id, status="printing")
    async with sessions() as session:
        row = await session.get(PrintQueueItem, owner_id)
        row.status = "completed"  # what the closer leaves behind
        row.completed_at = clockface.at(-3600)
        await session.commit()

    await assert_owned_forever(sessions, source, clockface)


async def test_a_blob_nobody_names_is_marked_and_then_released_after_the_grace(tmp_path, sessions, clockface):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    on_disk = object_of(source)

    first = await collect(sessions, clockface.at(0))
    assert (first.marked, first.released) == (1, 0)
    assert await mark_of(sessions, source.id) == clockface.at(0)
    assert on_disk.is_file()

    inside = await collect(sessions, clockface.at(queue_sources.ORPHAN_GRACE_SECONDS - 1))
    assert (inside.marked, inside.released) == (0, 0)
    assert on_disk.is_file()

    past = await collect(sessions, clockface.past_grace())
    assert past.released == 1
    assert not on_disk.exists()
    assert await rows_left(sessions) == 0


async def test_a_blob_that_regains_an_owner_loses_its_grace_mark(tmp_path, sessions, clockface):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))
    assert await mark_of(sessions, source.id) is not None

    await add_owner(sessions, source.id)
    report = await collect(sessions, clockface.past_grace())

    assert (report.unmarked, report.released) == (1, 0)
    assert await mark_of(sessions, source.id) is None
    assert object_of(source).is_file()


async def test_the_grace_is_the_parked_constant_not_a_literal(tmp_path, sessions, clockface, monkeypatch):
    """§6 asks for one home for the numbers; this proves the GC reads it."""
    await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))
    monkeypatch.setattr(queue_sources, "ORPHAN_GRACE_SECONDS", 5.0)

    inside = await collect(sessions, clockface.at(4))
    assert inside.released == 0

    past = await collect(sessions, clockface.at(6))
    assert past.released == 1


# --------------------------------------------------------------------------- #
# Pins always beat the grace — S5
# --------------------------------------------------------------------------- #


async def test_a_reader_pin_beats_the_grace(tmp_path, sessions, clockface):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))

    async with queue_sources.pin(source.id):
        held = await collect(sessions, clockface.past_grace())
        assert held.released == 0
        assert object_of(source).is_file()
        assert await state_of(sessions, source.id) == STATE_READY

    after = await collect(sessions, clockface.past_grace())
    assert after.released == 1
    assert not object_of(source).exists()


async def test_a_backup_pin_beats_the_grace_for_every_blob(tmp_path, sessions, clockface):
    first = await publish_blob(sessions, tmp_path / "share" / "one.3mf", payload=b"one")
    second = await publish_blob(sessions, tmp_path / "share" / "two.3mf", payload=b"two")
    await collect(sessions, clockface.at(0))

    async with queue_sources.pin_backup():
        held = await collect(sessions, clockface.past_grace())
        assert held.released == 0
        assert len(objects()) == 2

    after = await collect(sessions, clockface.past_grace())
    assert after.released == 2
    assert objects() == []
    assert await rows_left(sessions) == 0
    assert first.id != second.id


async def test_a_writer_pin_on_a_staged_file_beats_the_grace(tmp_path, sessions, clockface):
    """An unpublished receipt owns its ``.part`` however old the file looks (S6)."""
    make_3mf(tmp_path / "share" / "lamp.3mf")
    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(
            path=tmp_path / "share" / "lamp.3mf", format=FORMAT_3MF, display_filename="lamp.3mf"
        )
    )
    age(receipt.staging_path, clockface.epoch(-10 * 86400))

    report = await collect(sessions, clockface.past_grace())

    assert report.orphan_parts == 0
    assert receipt.staging_path.is_file()
    await receipt.discard()


async def test_a_part_a_live_worker_is_still_writing_is_never_removed(tmp_path, sessions, clockface, monkeypatch):
    """The pin set alone does not cover this — spec §6's "не unlink .part з-під живого writer".

    ``_staging_pins`` gains a path only when the worker hands it over. While the
    copy is running the file exists and is pinned by nothing, so a GC pass that
    read only that set would unlink a live SMB copy's staging file. A capture over
    a slow share can easily outlive the one-hour grace, and the restart sweep
    waives the grace for ``.part`` files altogether.

    ⚠️ It asserts the unlink was never **attempted**, not merely that the file
    survived. Windows refuses to unlink a file the worker still has open, so the
    weaker assertion passes on this platform for a reason that does not exist on
    Linux — where the unlink succeeds, the worker writes on into a deleted inode
    and ``os.replace`` then fails on a missing source. (Measured: dropping the
    ``_inflight`` half of ``staging_in_use`` left every assertion green.)
    """
    source = tmp_path / "share" / "lamp.3mf"
    raw = make_3mf(source)
    release = threading.Event()
    reading = threading.Event()
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
        queue_sources.capture(queue_sources.CaptureRequest(path=source, format=FORMAT_3MF, display_filename="lamp.3mf"))
    )
    await wait_for(reading.is_set)
    part = staged_parts()
    assert len(part) == 1
    age(part[0], clockface.epoch(-10 * 86400))

    report = await collect(sessions, clockface.past_grace(), part_grace_seconds=0.0)

    assert attempted == []
    assert (report.orphan_parts, report.failed) == (0, 0)
    assert staged_parts() == part
    release.set()
    receipt = await task
    await receipt.discard()


# --------------------------------------------------------------------------- #
# The count → unlink race — S5, and the guard is what closes it
# --------------------------------------------------------------------------- #


async def a_clone(sessions, source_id: int) -> bool:
    """What a second owner must look like: ask under the guard, then write.

    §9 puts clone on the same guard as finalize, attach, delete and the GC. The
    row is read first, so a clone that lost the race to a release sees the blob
    gone and refuses instead of writing a reference to nothing.
    """
    async with queue_sources.storage_mutation(), sessions() as session:
        row = await session.get(QueueSource, source_id)
        if row is None or row.state != STATE_READY:
            return False
        session.add(PrintQueueItem(queue_id=1, status="pending", queue_source_id=source_id))
        await session.commit()
        return True


async def test_a_reference_cannot_be_created_inside_the_unlink_window(tmp_path, sessions, clockface, monkeypatch):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))
    started, release = threading.Event(), threading.Event()
    real_unlink = queue_sources._unlink_blob

    def blocking_unlink(path: Path) -> None:
        started.set()
        release.wait(10)
        real_unlink(path)

    monkeypatch.setattr(queue_sources, "_unlink_blob", blocking_unlink)
    sweep = asyncio.create_task(collect(sessions, clockface.past_grace()))
    await wait_for(started.is_set)
    # The tombstone is already durable, so a crash here leaves work for the next
    # pass rather than a ready row with no file.
    assert await state_of(sessions, source.id) == STATE_DELETING

    clone = asyncio.create_task(a_clone(sessions, source.id))
    for _ in range(20):
        await asyncio.sleep(0.005)
    assert not clone.done()  # the guard holds every other writer outside the window

    release.set()
    report = await sweep
    assert report.released == 1
    assert await clone is False  # it asked, found the blob released, and refused
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PrintQueueItem)) == 0


async def test_a_reference_taken_before_the_guard_is_granted_saves_the_blob(tmp_path, sessions, clockface):
    """The re-check under the guard, not the mark, is what decides.

    ``unreferenced_at`` says only when the row was last *seen* unowned (§4). Here
    the mark is stamped and past its grace, and a clone then wins the guard: the
    pass that follows must ask both tables again rather than trust the hint.
    """
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))

    async with queue_sources.storage_mutation():
        sweep = asyncio.create_task(collect(sessions, clockface.past_grace()))
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert not sweep.done()
        async with sessions() as session:
            session.add(PrintQueueItem(queue_id=1, status="pending", queue_source_id=source.id))
            await session.commit()

    report = await sweep
    assert (report.released, report.unmarked) == (0, 1)
    assert object_of(source).is_file()
    assert await mark_of(sessions, source.id) is None


# --------------------------------------------------------------------------- #
# Tombstones — an unlink that failed keeps its row for the next pass
# --------------------------------------------------------------------------- #


async def test_an_unlink_failure_leaves_a_tombstone_the_next_pass_retries(tmp_path, sessions, clockface, monkeypatch):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))
    real_unlink = queue_sources._unlink_blob
    monkeypatch.setattr(
        queue_sources, "_unlink_blob", lambda path: (_ for _ in ()).throw(OSError("the volume went away"))
    )

    failed = await collect(sessions, clockface.past_grace())

    assert (failed.released, failed.failed) == (0, 1)
    assert await state_of(sessions, source.id) == STATE_DELETING
    assert object_of(source).is_file()

    monkeypatch.setattr(queue_sources, "_unlink_blob", real_unlink)
    # No new grace is served: the row's deletion was already decided.
    retried = await collect(sessions, clockface.at(0))

    assert (retried.tombstones, retried.released) == (1, 1)
    assert await rows_left(sessions) == 0
    assert not object_of(source).exists()


async def test_a_tombstone_whose_blob_regained_an_owner_is_not_unlinked(tmp_path, sessions, clockface, monkeypatch):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))
    real_unlink = queue_sources._unlink_blob
    monkeypatch.setattr(
        queue_sources, "_unlink_blob", lambda path: (_ for _ in ()).throw(OSError("the volume went away"))
    )
    await collect(sessions, clockface.past_grace())
    assert await state_of(sessions, source.id) == STATE_DELETING
    # Restored by re-patching, never by ``monkeypatch.undo()``: that instance is
    # shared with ``data_dir_isolation``, so undoing here would put
    # ``settings.data_dir`` back on the developer's live ``data/`` folder.
    monkeypatch.setattr(queue_sources, "_unlink_blob", real_unlink)

    await add_owner(sessions, source.id)
    report = await collect(sessions, clockface.past_grace())

    # The bytes are intact and somebody owns them again, so the row is honest
    # about that instead of being unlinked under its new owner.
    assert (report.released, report.revived) == (0, 1)
    assert await state_of(sessions, source.id) == STATE_READY
    assert object_of(source).is_file()


async def test_a_tombstone_with_an_owner_and_no_bytes_becomes_broken_not_ready(tmp_path, sessions, clockface):
    """§9: a race must not revive a job on a vanished file."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    async with sessions() as session:
        row = await session.get(QueueSource, source.id)
        row.state = STATE_DELETING
        await session.commit()
    object_of(source).unlink()
    await add_owner(sessions, source.id)

    report = await collect(sessions, clockface.past_grace())

    assert (report.released, report.broken) == (0, 1)
    assert await state_of(sessions, source.id) == STATE_BROKEN
    assert await rows_left(sessions) == 1


async def test_a_broken_blob_with_an_owner_is_never_evicted(tmp_path, sessions, clockface):
    """§9: "broken referenced файли не евікати" — the reference is the diagnosis."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    async with sessions() as session:
        row = await session.get(QueueSource, source.id)
        row.state = STATE_BROKEN
        await session.commit()
    await add_owner(sessions, source.id, status="failed")

    report = await collect(sessions, clockface.past_grace(extra=10 * 86400))

    assert report.released == 0
    assert await state_of(sessions, source.id) == STATE_BROKEN
    assert object_of(source).is_file()


async def test_a_broken_blob_nobody_owns_is_released_after_the_grace(tmp_path, sessions, clockface):
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    async with sessions() as session:
        row = await session.get(QueueSource, source.id)
        row.state = STATE_BROKEN
        await session.commit()

    await collect(sessions, clockface.at(0))
    report = await collect(sessions, clockface.past_grace())

    assert report.released == 1
    assert await rows_left(sessions) == 0


async def test_the_gc_never_hashes_a_ready_object(tmp_path, sessions, clockface, monkeypatch):
    """§7: "Не хешувати великий файл у кожному scheduler tick"."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await add_owner(sessions, source.id)
    verified: list[Path] = []
    real_verify = queue_sources._verify_object
    monkeypatch.setattr(
        queue_sources,
        "_verify_object",
        lambda target, sha, size: (verified.append(target), real_verify(target, sha, size))[1],
    )

    await collect(sessions, clockface.past_grace())

    assert verified == []


# --------------------------------------------------------------------------- #
# Orphan discovery — inside our own root, never through a link
# --------------------------------------------------------------------------- #


async def test_an_object_with_no_row_is_released_only_after_the_grace(tmp_path, sessions, clockface):
    """The leftover a publication that died after its rename leaves (§5 step 7)."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    orphan = object_of(source)
    async with sessions() as session:
        await session.delete(await session.get(QueueSource, source.id))
        await session.commit()
    age(orphan, clockface.epoch(-queue_sources.ORPHAN_GRACE_SECONDS + 30))

    inside = await collect(sessions, clockface.at(0))
    assert inside.orphan_objects == 0
    assert orphan.is_file()

    age(orphan, clockface.epoch(-queue_sources.ORPHAN_GRACE_SECONDS - 30))
    past = await collect(sessions, clockface.at(0))
    assert past.orphan_objects == 1
    assert not orphan.exists()


async def test_a_stray_part_with_no_writer_is_released_after_the_grace(tmp_path, sessions, clockface):
    queue_sources.staging_root().mkdir(parents=True, exist_ok=True)
    stray = queue_sources.staging_root() / "deadbeef.part"
    stray.write_bytes(b"half a file")
    age(stray, clockface.epoch(-30))

    inside = await collect(sessions, clockface.at(0))
    assert inside.orphan_parts == 0

    age(stray, clockface.epoch(-queue_sources.ORPHAN_GRACE_SECONDS - 30))
    past = await collect(sessions, clockface.at(0))
    assert past.orphan_parts == 1
    assert not stray.exists()


async def test_orphan_discovery_stays_inside_the_spool_and_never_follows_a_link(tmp_path, sessions, clockface):
    """§9: "не ходити за symlink/junction і не торкатися інших data trees"."""
    outside = tmp_path / "somebody-elses-tree"
    outside.mkdir()
    treasure = outside / "important.3mf"
    treasure.write_bytes(b"not ours to delete")
    age(treasure, clockface.epoch(-10 * 86400))
    queue_sources.objects_root().mkdir(parents=True, exist_ok=True)
    make_link(queue_sources.objects_root() / "aa", outside)

    report = await collect(sessions, clockface.at(0))

    assert report.skipped_links == 1
    assert report.orphan_objects == 0
    assert treasure.read_bytes() == b"not ours to delete"


async def test_a_file_at_an_unexpected_depth_is_reported_not_deleted(tmp_path, sessions, clockface):
    """A GC deletes only what it writes: sharded objects and ``.part`` files."""
    queue_sources.objects_root().mkdir(parents=True, exist_ok=True)
    queue_sources.staging_root().mkdir(parents=True, exist_ok=True)
    loose = queue_sources.objects_root() / "loose.3mf"
    loose.write_bytes(b"who put this here")
    notes = queue_sources.staging_root() / "notes.txt"
    notes.write_bytes(b"not a part file")
    for path in (loose, notes):
        age(path, clockface.epoch(-10 * 86400))

    report = await collect(sessions, clockface.at(0))

    assert report.skipped_unexpected == 2
    assert (report.orphan_objects, report.orphan_parts) == (0, 0)
    assert loose.is_file()
    assert notes.is_file()


# --------------------------------------------------------------------------- #
# The interval, and where the work happens
# --------------------------------------------------------------------------- #


async def test_the_gc_runs_no_more_often_than_its_minimum_interval(tmp_path, sessions, clockface, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(queue_sources, "_now", clock)
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")

    first = await queue_sources.collect(now=clockface.at(0), session_factory=sessions)
    assert first.throttled is False
    assert await mark_of(sessions, source.id) is not None

    second = await queue_sources.collect(now=clockface.past_grace(), session_factory=sessions)
    assert second.throttled is True
    assert second.released == 0
    assert object_of(source).is_file()

    forced = await queue_sources.collect(now=clockface.past_grace(), session_factory=sessions, force=True)
    assert forced.released == 1

    again = await publish_blob(sessions, tmp_path / "share" / "other.3mf", payload=b"other")
    await queue_sources.collect(now=clockface.at(0), session_factory=sessions)
    assert await mark_of(sessions, again.id) is None  # still throttled

    clock.advance(queue_sources.GC_MIN_INTERVAL_SECONDS)
    await queue_sources.collect(now=clockface.at(0), session_factory=sessions)
    assert await mark_of(sessions, again.id) is not None


async def test_the_gc_does_every_filesystem_step_off_the_loop(tmp_path, sessions, clockface, monkeypatch):
    """S10 — no long file I/O on the asyncio loop, GC included (§6)."""
    source = await publish_blob(sessions, tmp_path / "share" / "lamp.3mf")
    await collect(sessions, clockface.at(0))
    on_main: list[bool] = []
    real_unlink, real_scan = queue_sources._unlink_blob, queue_sources._scan_spool

    def spy_unlink(path: Path) -> None:
        on_main.append(threading.current_thread() is threading.main_thread())
        real_unlink(path)

    def spy_scan(known):
        on_main.append(threading.current_thread() is threading.main_thread())
        return real_scan(known)

    monkeypatch.setattr(queue_sources, "_unlink_blob", spy_unlink)
    monkeypatch.setattr(queue_sources, "_scan_spool", spy_scan)

    report = await collect(sessions, clockface.past_grace())

    assert report.released == 1
    assert len(on_main) == 2
    assert not any(on_main)
    assert source.id


# --------------------------------------------------------------------------- #
# The module must leave nothing behind
# --------------------------------------------------------------------------- #


async def test_the_loop_and_the_default_executor_are_still_healthy():
    assert await asyncio.to_thread(lambda: "shared executor alive") == "shared executor alive"
    assert asyncio.get_running_loop().is_running()
    assert queue_sources.active_captures() == 0
    leaked = [t.name for t in threading.enumerate() if t.name.startswith(queue_sources.THREAD_NAME_PREFIX)]
    assert leaked == []
