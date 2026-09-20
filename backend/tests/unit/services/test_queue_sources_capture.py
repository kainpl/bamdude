"""Capture and publication of a queue source — spec §5's seven steps.

Spec: ``60-specs/queue-source-spool-spec.md``. Everything here is about the two
halves of §5: the worker that copies and hashes the bytes off the event loop,
and the supervisor that publishes them atomically (rename → row → the caller's
attach → commit, in that order, under one guard).

No test waits for a real deadline: the clock is a fake and a stalled read is a
``threading.Event``. The last test in the module asserts the loop and the shared
executor survived, because a leaked capture thread is a defect and not noise.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.queue_source import (
    FORMAT_3MF,
    FORMAT_GCODE,
    STATE_BROKEN,
    STATE_DELETING,
    STATE_READY,
    QueueSource,
)
from backend.app.services import queue_sources
from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION, source_snapshot

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def make_3mf(path: Path, *, payload: bytes = b"<model/>", compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    """A small but genuine 3MF container (a ZIP whose central directory is last)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compress) as zf:
        zf.writestr("3D/3dmodel.model", payload)
        zf.writestr("Metadata/plate_1.png", b"\x89PNG" + b"\x00" * 64)
    return path.read_bytes()


@pytest.fixture(autouse=True)
def clean_spool_state():
    """Process-global capture state must not travel between tests."""
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine):
    """A session factory on the test engine — publish owns its own transaction."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def request_for(path: Path, fmt: str = FORMAT_3MF, **kwargs) -> queue_sources.CaptureRequest:
    return queue_sources.CaptureRequest(path=path, format=fmt, display_filename=path.name, **kwargs)


def staged_parts() -> list[Path]:
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.exists() else []


def objects() -> list[Path]:
    root = queue_sources.objects_root()
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


async def wait_for(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:  # pragma: no cover - a failed wait fails the test below
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


def recording_attach(seen: list):
    async def attach(session: AsyncSession, source: QueueSource) -> None:
        seen.append(source)

    return attach


async def publish_bytes(sessions, path: Path, fmt: str = FORMAT_3MF) -> QueueSource:
    receipt = await queue_sources.capture(request_for(path, fmt))
    return await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)


# --------------------------------------------------------------------------- #
# Step 2 + 3 — the worker opens the original, hashes it, stages it
# --------------------------------------------------------------------------- #


async def test_a_capture_stages_the_bytes_and_hashes_them_off_the_loop(tmp_path, monkeypatch):
    source = tmp_path / "share" / "lamp.3mf"
    raw = make_3mf(source)
    threads: list[bool] = []
    real_open = queue_sources._open_source
    real_stat = queue_sources._stat_source

    def spy_open(path):
        threads.append(threading.current_thread() is threading.main_thread())
        return real_open(path)

    def spy_stat(path):
        threads.append(threading.current_thread() is threading.main_thread())
        return real_stat(path)

    monkeypatch.setattr(queue_sources, "_open_source", spy_open)
    monkeypatch.setattr(queue_sources, "_stat_source", spy_stat)

    receipt = await queue_sources.capture(request_for(source))

    assert receipt.sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.size_bytes == len(raw)
    assert receipt.format == FORMAT_3MF
    assert receipt.staging_path.parent == queue_sources.staging_root()
    assert receipt.staging_path.suffix == ".part"
    assert receipt.staging_path.read_bytes() == raw
    # S10 + §5.2: not one stat, open or read of the original happened on the loop.
    assert threads and not any(threads)
    assert source.read_bytes() == raw
    assert queue_sources.active_captures() == 0


async def test_a_capture_is_not_yet_a_queue_source(tmp_path, sessions):
    """Staging is not a ready QueueSource and cannot back a pending job (§4)."""
    receipt = await queue_sources.capture(request_for(_three(tmp_path)))

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 0
    assert receipt.staging_path.is_file()
    assert staged_parts() == [receipt.staging_path]
    assert objects() == []


def _three(tmp_path: Path) -> Path:
    source = tmp_path / "share" / "plate.3mf"
    make_3mf(source)
    return source


# --------------------------------------------------------------------------- #
# Steps 5–7 — publication
# --------------------------------------------------------------------------- #


async def test_publishing_renames_the_staged_file_into_its_sharded_object_path(tmp_path, sessions):
    source = _three(tmp_path)
    raw = source.read_bytes()
    receipt = await queue_sources.capture(request_for(source))
    staged = receipt.staging_path
    seen: list[QueueSource] = []

    published = await queue_sources.publish(receipt, recording_attach(seen), session_factory=sessions)

    sha = hashlib.sha256(raw).hexdigest()
    assert published.sha256 == sha
    assert published.size_bytes == len(raw)
    assert published.state == STATE_READY
    assert published.relative_path == f"queue-sources/objects/{sha[:2]}/{sha}.3mf"
    on_disk = Path(settings.base_dir) / published.relative_path
    assert on_disk.read_bytes() == raw
    assert not staged.exists()
    assert staged_parts() == []
    assert [row.id for row in seen] == [published.id]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 1


async def test_two_concurrent_captures_of_identical_bytes_publish_one_row_and_one_file(tmp_path, sessions):
    first = tmp_path / "share" / "a" / "same.3mf"
    second = tmp_path / "share" / "b" / "other-name.3mf"
    raw = make_3mf(first)
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(raw)

    receipts = await asyncio.gather(
        queue_sources.capture(request_for(first)),
        queue_sources.capture(request_for(second)),
    )
    assert receipts[0].staging_path != receipts[1].staging_path
    seen: list[QueueSource] = []
    published = await asyncio.gather(
        queue_sources.publish(receipts[0], recording_attach(seen), session_factory=sessions),
        queue_sources.publish(receipts[1], recording_attach(seen), session_factory=sessions),
    )

    assert published[0].id == published[1].id
    assert len(seen) == 2
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 1
    assert len(objects()) == 1
    assert staged_parts() == []


async def test_reuse_of_a_ready_hash_keeps_the_existing_file_and_row(tmp_path, sessions):
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    os.utime(on_disk, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    before = on_disk.stat().st_mtime_ns

    again = await publish_bytes(sessions, source)

    assert again.id == existing.id
    # The existing object was verified, not replaced: a rename would have
    # carried the staged file's own timestamps in with it.
    assert on_disk.stat().st_mtime_ns == before
    assert len(objects()) == 1
    assert staged_parts() == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 1


async def test_a_ready_row_whose_file_vanished_is_repaired_from_the_captured_bytes(tmp_path, sessions):
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    on_disk.unlink()

    repaired = await publish_bytes(sessions, source)

    assert repaired.id == existing.id
    assert repaired.state == STATE_READY
    assert on_disk.read_bytes() == source.read_bytes()


async def test_a_broken_row_is_repaired_by_the_same_bytes(tmp_path, sessions):
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    # Corrupted WITHOUT changing the length: only the hash can tell, which is
    # why §7 asks for the full digest at reuse and not a stat.
    on_disk.write_bytes(b"\x00" * existing.size_bytes)
    assert on_disk.stat().st_size == existing.size_bytes
    async with sessions() as session:
        row = await session.get(QueueSource, existing.id)
        row.state = STATE_BROKEN
        await session.commit()

    repaired = await publish_bytes(sessions, source)

    assert repaired.id == existing.id
    assert repaired.state == STATE_READY
    assert on_disk.read_bytes() == source.read_bytes()
    async with sessions() as session:
        assert (await session.get(QueueSource, existing.id)).state == STATE_READY


async def test_a_broken_row_with_a_live_pin_answers_busy_and_stays_broken(tmp_path, sessions):
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    on_disk.write_bytes(b"corrupt")
    async with sessions() as session:
        row = await session.get(QueueSource, existing.id)
        row.state = STATE_BROKEN
        await session.commit()

    receipt = await queue_sources.capture(request_for(source))
    async with queue_sources.pin(existing.id):
        with pytest.raises(queue_sources.QueueSourceBusy):
            await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)

    # Nothing was written under the pin, and the caller keeps its bytes.
    assert on_disk.read_bytes() == b"corrupt"
    assert receipt.staging_path.is_file()
    async with sessions() as session:
        assert (await session.get(QueueSource, existing.id)).state == STATE_BROKEN
    # With the pin gone the same receipt repairs the row.
    repaired = await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)
    assert repaired.id == existing.id
    assert on_disk.read_bytes() == source.read_bytes()


async def test_a_deleting_row_answers_busy_rather_than_overwriting_under_an_unlink(tmp_path, sessions):
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    async with sessions() as session:
        row = await session.get(QueueSource, existing.id)
        row.state = STATE_DELETING
        await session.commit()

    receipt = await queue_sources.capture(request_for(source))
    with pytest.raises(queue_sources.QueueSourceBusy):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)

    assert on_disk.read_bytes() == source.read_bytes()
    assert receipt.staging_path.is_file()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 1
        assert (await session.get(QueueSource, existing.id)).state == STATE_DELETING


async def test_a_deleting_row_whose_file_is_already_gone_is_not_written_again(tmp_path, sessions):
    """The case the guard actually prevents a write in.

    The GC has already unlinked the object and is still finishing its own work.
    Without the ``deleting`` check the verification would simply say "missing"
    and the repair branch would put a file back underneath it — spec §5 step 5's
    "не підміняти файл під незавершеним unlink".
    """
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    on_disk.unlink()
    async with sessions() as session:
        row = await session.get(QueueSource, existing.id)
        row.state = STATE_DELETING
        await session.commit()

    receipt = await queue_sources.capture(request_for(source))
    with pytest.raises(queue_sources.QueueSourceBusy):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)

    assert not on_disk.exists()
    assert objects() == []
    # Busy is not a spent receipt: nothing moved, so the caller may retry.
    assert receipt.staging_path.is_file()
    async with sessions() as session:
        assert (await session.get(QueueSource, existing.id)).state == STATE_DELETING


async def test_a_row_pointing_outside_the_spool_is_refused_not_written(tmp_path, sessions):
    """A path is never trusted because a row carries it (§4: no arbitrary path)."""
    source = _three(tmp_path)
    receipt = await queue_sources.capture(request_for(source))
    async with sessions() as session:
        session.add(
            QueueSource(
                sha256=receipt.sha256,
                size_bytes=receipt.size_bytes,
                relative_path="../escaped.3mf",
                format=FORMAT_3MF,
                state=STATE_READY,
            )
        )
        await session.commit()

    with pytest.raises(queue_sources.WriteFailed):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)

    assert not (Path(settings.base_dir).parent / "escaped.3mf").exists()
    # A ``WriteFailed`` out of the install proves nothing moved, so this receipt
    # is still good — bytes, pin and the right to publish all intact.
    assert receipt.staging_path.is_file()
    assert queue_sources.pinned_staging_paths() == frozenset({receipt.staging_path})
    async with sessions() as session:
        bad = await session.scalar(select(QueueSource).where(QueueSource.sha256 == receipt.sha256))
        await session.delete(bad)
        await session.commit()

    published = await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)
    assert published.relative_path == queue_sources.object_relative_path(receipt.sha256, FORMAT_3MF)


async def test_attach_failure_leaves_no_row_and_no_final_file(tmp_path, sessions):
    source = _three(tmp_path)
    receipt = await queue_sources.capture(request_for(source))

    async def attach(session: AsyncSession, row: QueueSource) -> None:
        raise RuntimeError("the caller's own refusal")

    with pytest.raises(RuntimeError, match="the caller's own refusal"):
        await queue_sources.publish(receipt, attach, session_factory=sessions)

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 0
    assert objects() == []
    # The staged file was consumed by the rename and then removed with the
    # object, so this receipt is spent. It must not advertise itself as
    # publishable (a retry would reach ``os.replace`` on a missing file and
    # escape the taxonomy), and its staging pin must not outlive it — that set
    # is what the GC reads.
    assert not receipt.staging_path.exists()
    assert queue_sources.pinned_staging_paths() == frozenset()
    with pytest.raises(RuntimeError, match="already"):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)


async def test_a_cancellation_inside_the_install_spends_the_receipt(tmp_path, sessions, monkeypatch):
    """``_file_work`` joins its worker and only THEN re-raises ``CancelledError``.

    So a client disconnect (or shutdown) can land while ``os.replace`` has
    already happened: measured on ``core/db_portable.py:79-96``. The receipt must
    be spent on that doubt, not revived over a file that has moved — a revived
    one would reach ``os.replace`` on nothing and escape the taxonomy as a bare
    ``OSError``, and its pin would sit in the GC's set with no owner left to
    clear it.
    """
    source = _three(tmp_path)
    receipt = await queue_sources.capture(request_for(source))
    started, release = threading.Event(), threading.Event()
    real_install = queue_sources._install_object

    def blocking_install(part: Path, target: Path) -> None:
        started.set()
        release.wait(10)
        real_install(part, target)  # the rename really does complete

    monkeypatch.setattr(queue_sources, "_install_object", blocking_install)
    task = asyncio.create_task(queue_sources.publish(receipt, recording_attach([]), session_factory=sessions))
    await wait_for(started.is_set)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The bytes did move — they are an orphan object with no row, which is
    # exactly what §5.7 hands to cleanup — so the receipt is spent.
    assert len(objects()) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 0
    assert queue_sources.pinned_staging_paths() == frozenset()
    with pytest.raises(RuntimeError, match="already"):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)


async def test_attach_failure_after_a_repair_also_spends_the_receipt(tmp_path, sessions):
    """The repair path consumes the staged file too (§5 step 5)."""
    source = _three(tmp_path)
    existing = await publish_bytes(sessions, source)
    on_disk = Path(settings.base_dir) / existing.relative_path
    on_disk.unlink()
    receipt = await queue_sources.capture(request_for(source))

    async def attach(session: AsyncSession, row: QueueSource) -> None:
        raise RuntimeError("the caller's own refusal")

    with pytest.raises(RuntimeError, match="the caller's own refusal"):
        await queue_sources.publish(receipt, attach, session_factory=sessions)

    # The repair stands: the bytes are correct and this publication did not
    # create the object, so its other owners keep it.
    assert on_disk.read_bytes() == source.read_bytes()
    assert not receipt.staging_path.exists()
    assert queue_sources.pinned_staging_paths() == frozenset()
    with pytest.raises(RuntimeError, match="already"):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)


async def test_the_snapshot_payload_comes_from_task_ones_builder(tmp_path, sessions):
    source = _three(tmp_path)
    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(
            path=source,
            format=FORMAT_3MF,
            display_filename="Нічник.3mf",
            plate_fallback=3,
            provenance={"kind": "library_file", "id": 9},
        )
    )
    published = await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)

    payload = queue_sources.snapshot_for(receipt, published)

    assert payload == source_snapshot(receipt.descriptor(published))
    assert payload == {
        "version": SOURCE_SNAPSHOT_VERSION,
        "provenance": {"kind": "library_file", "id": 9},
        "display_filename": "Нічник.3mf",
        "format": FORMAT_3MF,
        "plate_fallback": 3,
    }
    # §4: the hash and the path are not duplicated into the job's JSON.
    assert published.sha256 not in str(payload)
    assert "queue-sources" not in str(payload)
    descriptor = receipt.descriptor(published)
    assert descriptor.path == Path(settings.base_dir) / published.relative_path
    assert descriptor.sha256 == published.sha256


# --------------------------------------------------------------------------- #
# Step 3's validation — a half file never becomes a runnable job (A05)
# --------------------------------------------------------------------------- #


async def test_a_truncated_3mf_is_refused(tmp_path):
    source = tmp_path / "share" / "torn.3mf"
    raw = make_3mf(source)
    source.write_bytes(raw[:-40])

    with pytest.raises(queue_sources.SourceInvalid):
        await queue_sources.capture(request_for(source))

    assert staged_parts() == []
    assert queue_sources.active_captures() == 0


async def test_a_crc_corrupt_3mf_is_refused(tmp_path):
    source = tmp_path / "share" / "rotten.3mf"
    make_3mf(source, payload=b"A" * 400, compress=zipfile.ZIP_STORED)
    raw = bytearray(source.read_bytes())
    index = raw.find(b"A" * 400)
    assert index > 0
    raw[index + 10] ^= 0xFF
    source.write_bytes(bytes(raw))

    with pytest.raises(queue_sources.SourceInvalid):
        await queue_sources.capture(request_for(source))


async def test_a_gcode_that_is_a_zip_container_is_refused(tmp_path):
    source = tmp_path / "share" / "lying.gcode"
    make_3mf(source)

    with pytest.raises(queue_sources.SourceInvalid):
        await queue_sources.capture(request_for(source, FORMAT_GCODE))


async def test_raw_gcode_is_captured_as_itself(tmp_path):
    source = tmp_path / "share" / "real.gcode"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"G28\nG1 X10 Y10\n")

    receipt = await queue_sources.capture(request_for(source, FORMAT_GCODE))

    assert receipt.format == FORMAT_GCODE
    assert receipt.staging_path.read_bytes() == source.read_bytes()


async def test_an_empty_source_is_refused(tmp_path):
    source = tmp_path / "share" / "empty.gcode"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"")

    with pytest.raises(queue_sources.SourceInvalid):
        await queue_sources.capture(request_for(source, FORMAT_GCODE))


async def test_a_source_replaced_while_it_was_read_is_refused(tmp_path, monkeypatch):
    source = _three(tmp_path)
    other = tmp_path / "share" / "different.3mf"
    make_3mf(other, payload=b"a different model entirely")
    real_stat = queue_sources._stat_source
    calls = {"n": 0}

    def swapping_stat(path):
        calls["n"] += 1
        return real_stat(path if calls["n"] == 1 else other)

    monkeypatch.setattr(queue_sources, "_stat_source", swapping_stat)

    with pytest.raises(queue_sources.SourceChanged):
        await queue_sources.capture(request_for(source))

    assert staged_parts() == []


async def test_an_incomplete_read_is_refused(tmp_path, monkeypatch):
    source = _three(tmp_path)
    short = source.read_bytes()[:20]

    class ShortHandle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def read(self, size):
            nonlocal short
            chunk, short = short[:size], short[size:]
            return chunk

    monkeypatch.setattr(queue_sources, "_open_source", lambda path: ShortHandle())

    with pytest.raises(queue_sources.SourceChanged):
        await queue_sources.capture(request_for(source))


# --------------------------------------------------------------------------- #
# The receipt's own rules
# --------------------------------------------------------------------------- #


async def test_a_receipt_from_a_replaced_spool_cannot_be_published(tmp_path, sessions):
    source = _three(tmp_path)
    receipt = await queue_sources.capture(request_for(source))
    assert receipt.epoch == queue_sources.current_epoch()

    queue_sources.invalidate_receipts()

    with pytest.raises(queue_sources.QueueSourceBusy):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QueueSource)) == 0


async def test_a_receipt_can_only_be_published_once(tmp_path, sessions):
    source = _three(tmp_path)
    receipt = await queue_sources.capture(request_for(source))
    await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)

    with pytest.raises(RuntimeError):
        await queue_sources.publish(receipt, recording_attach([]), session_factory=sessions)


async def test_a_discarded_receipt_leaves_no_staged_bytes(tmp_path):
    source = _three(tmp_path)
    receipt = await queue_sources.capture(request_for(source))
    staged = receipt.staging_path

    await receipt.discard()

    assert not staged.exists()
    assert receipt.staging_path not in queue_sources.pinned_staging_paths()


# --------------------------------------------------------------------------- #
# The module must leave nothing behind
# --------------------------------------------------------------------------- #


async def test_the_loop_and_the_default_executor_are_still_healthy():
    assert await asyncio.to_thread(lambda: "shared executor alive") == "shared executor alive"
    assert asyncio.get_running_loop().is_running()
    assert queue_sources.active_captures() == 0
    leaked = [t.name for t in threading.enumerate() if t.name.startswith(queue_sources.THREAD_NAME_PREFIX)]
    assert leaked == []
