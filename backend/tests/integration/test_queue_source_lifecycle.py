"""The other end of a queue source's life — spec §9 and §10.

Spec: ``60-specs/queue-source-spool-spec.md`` (A09, A18, S5). Three questions,
and the feature is judged on the first one:

1. **The original is deleted.** Library trash, the retention sweeper, an external
   file's purge, the scanner finding a vanished share, an archive's trash and its
   hard delete all detach the navigational link and **leave the work standing** —
   its status, its position, its order line and its name. A job that still needs
   the original is cancelled with a reason, visibly, as it always was.
2. **A job row goes away.** The reference is freed exactly once, on every path,
   the router half included — and a ``printing`` row closed on age is not a
   completion, so it keeps its bytes.
3. **A print completes.** Before the row is tidied away, the print's own archive
   bytes are confirmed to exist independently of the spool; when they cannot be,
   the row and its reference stay with a visible diagnostic and the archive
   pipeline retries — never a second ``PrintArchive`` for one physical print.

⚠️ **Nothing here fakes the collector.** "The blob survived" is asserted by
running a real :func:`queue_sources.collect` pass past the grace window and
looking at the file, because the only thing that can actually unlink those bytes
is the GC, and the only thing that stops it is a row in one of the two queues.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import (
    FORMAT_3MF,
    STATE_BROKEN,
    STATE_DELETING,
    STATE_READY,
    QueueSource,
)
from backend.app.services import queue_sources
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor, source_snapshot

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


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
def sessions(test_engine, monkeypatch):
    """The test engine, wherever a service opens its own session.

    ``queue_sources.publish``, the collector and ``library_scan.remove_vanished``
    all own their transactions, so they read ``database.async_session`` rather
    than a session handed in.
    """
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def make_3mf(path: Path, *, payload: bytes = b"<model/>") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", payload)
        zf.writestr("Metadata/plate_1.png", b"\x89PNG" + b"\x00" * 64)
    return path


async def a_ready_blob(sessions, path: Path, *, payload: bytes = b"<model/>") -> QueueSource:
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


async def a_queue(db, printer_id: int) -> PrinterQueue:
    """The printer's own queue row — its id IS the printer's id by invariant."""
    existing = await db.get(PrinterQueue, printer_id)
    if existing is not None:
        return existing
    queue = PrinterQueue(id=printer_id, printer_id=printer_id, status="idle")
    db.add(queue)
    await db.flush()
    return queue


async def a_job(
    db,
    *,
    queue_id: int,
    blob: QueueSource | None = None,
    archive_id: int | None = None,
    library_file_id: int | None = None,
    status: str = "pending",
    **columns: Any,
) -> PrintQueueItem:
    columns.setdefault("position", 1)
    item = PrintQueueItem(
        queue_id=queue_id,
        status=status,
        archive_id=archive_id,
        library_file_id=library_file_id,
        queue_source_id=blob.id if blob is not None else None,
        source_snapshot=_snapshot(blob, library_file_id=library_file_id, archive_id=archive_id)
        if blob is not None
        else None,
        **columns,
    )
    db.add(item)
    await db.flush()
    return item


async def an_auto_job(
    db,
    *,
    blob: QueueSource | None = None,
    archive_id: int | None = None,
    library_file_id: int | None = None,
    status: str = "pending",
    **columns: Any,
) -> AutoQueueItem:
    columns.setdefault("position", 1)
    item = AutoQueueItem(
        status=status,
        archive_id=archive_id,
        library_file_id=library_file_id,
        queue_source_id=blob.id if blob is not None else None,
        source_snapshot=_snapshot(blob, library_file_id=library_file_id, archive_id=archive_id)
        if blob is not None
        else None,
        **columns,
    )
    db.add(item)
    await db.flush()
    return item


def _snapshot(blob: QueueSource, *, library_file_id: int | None, archive_id: int | None) -> dict:
    """The payload a real capture would have written — through the one builder."""
    kind, ident = ("archive", archive_id) if archive_id is not None else ("library_file", library_file_id)
    return source_snapshot(
        QueueSourceDescriptor(
            path=object_of(blob),
            format=blob.format,
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            display_filename="lamp.gcode.3mf",
            provenance={"kind": kind, "id": ident},
            queue_source_id=blob.id,
        )
    )


async def a_library_file(db, tmp_path, *, external: bool = False, name: str = "lamp.gcode.3mf") -> LibraryFile:
    path = make_3mf(tmp_path / "share" / name)
    row = LibraryFile(
        filename=name,
        file_path=str(path),
        file_type="3mf",
        file_size=path.stat().st_size,
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        is_external=external,
    )
    db.add(row)
    await db.flush()
    return row


async def an_archive_with_its_own_bytes(db, printer_id: int, *, name: str = "shelf.gcode.3mf") -> PrintArchive:
    """An archive row whose 3MF is where ``archive_print`` would have put it."""
    folder = settings.archive_dir / "lifecycle"
    path = make_3mf(folder / name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = PrintArchive(
        printer_id=printer_id,
        filename=name,
        print_name="Shelf",
        file_path=str(path.relative_to(settings.base_dir)),
        file_size=path.stat().st_size,
        content_hash=digest,
        source_content_hash=digest,
        status="completed",
    )
    db.add(row)
    await db.flush()
    return row


class Clockface:
    """The wall clock the grace is measured against, anchored to the real one.

    Anchored rather than fixed: a published object's mtime comes from the
    filesystem, so a hardcoded date would make the arithmetic depend on the day
    the suite is run.
    """

    def __init__(self) -> None:
        self.anchor = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    def at(self, offset: float = 0.0) -> datetime:
        return self.anchor + timedelta(seconds=offset)

    def past_grace(self, extra: float = 60.0) -> datetime:
        return self.at(queue_sources.ORPHAN_GRACE_SECONDS + extra)


@pytest.fixture
def clockface() -> Clockface:
    return Clockface()


async def sweep_twice(sessions, clockface: Clockface):
    """Mark, then release: two real GC passes, the second past the grace."""
    await queue_sources.collect(clockface.at(0), session_factory=sessions, force=True)
    return await queue_sources.collect(clockface.past_grace(), session_factory=sessions, force=True)


async def assert_still_owned(sessions, blob: QueueSource, clockface: Clockface) -> None:
    """The GC, asked twice past the grace, still refuses to release these bytes."""
    report = await sweep_twice(sessions, clockface)
    assert report.released == 0, "the collector released a blob a job still names"
    assert object_of(blob).is_file(), "the bytes a job still names were unlinked"
    async with sessions() as session:
        assert await session.get(QueueSource, blob.id) is not None


async def assert_released(sessions, blob: QueueSource, clockface: Clockface) -> None:
    """Nobody owns these bytes any more, so the collector takes them."""
    report = await sweep_twice(sessions, clockface)
    assert report.released == 1, "the reference was not freed — the collector still sees an owner"
    assert not object_of(blob).exists()
    async with sessions() as session:
        assert await session.get(QueueSource, blob.id) is None


async def reread(db, item):
    """The row as the database now holds it, or ``None`` if it is gone.

    ⚠️ ``expunge_all``, not ``expire_all``. The services under test commit through
    their own sessions, so the identity map has to be dropped — but an *expired*
    instance re-reads on the next attribute access, and a refresh with no
    ``await`` in front of it is a ``MissingGreenlet`` rather than a lazy query.
    Detaching leaves the already-loaded ids readable and sends ``get`` to the
    database, which is what a second ``reread`` in the same test needs.
    """
    model, item_id = type(item), item.id
    db.expunge_all()
    return await db.get(model, item_id)


# --------------------------------------------------------------------------- #
# §10 — deleting the original keeps the work (A09)
# --------------------------------------------------------------------------- #


async def test_trashing_a_library_file_leaves_a_ready_job_alone(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """The promise five screens make: the queue keeps printing (A09).

    Status, position, order line and the job's own name all survive, and the
    navigational link survives too — a trashed file can be restored, so cutting
    the link here would be destroying information the operator can still undo.
    """
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="A1")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id, position=7)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    job = await reread(db_session, job)
    assert job.status == "pending"
    assert job.queue_source_id == blob.id
    assert job.position == 7
    assert job.library_file_id == file.id, "a trashed file is restorable — the link stays"
    assert job.source_snapshot["display_filename"] == "lamp.gcode.3mf"
    await assert_still_owned(sessions, blob, clockface)


async def test_trashing_a_library_file_still_cancels_a_legacy_job(db_session, tmp_path, printer_factory, sessions):
    """The other half, unchanged: a job that has no copy of its own cannot print,
    and says so where the operator can see it (#2819)."""
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="A2")
    queue = await a_queue(db_session, printer.id)
    file = await a_library_file(db_session, tmp_path)
    job = await a_job(db_session, queue_id=queue.id, library_file_id=file.id)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    job = await reread(db_session, job)
    assert job.status == "cancelled"
    assert job.waiting_reason == "Source file deleted"


async def test_trashing_a_library_file_fails_a_legacy_auto_row(db_session, tmp_path, printer_factory, sessions):
    """Both tiers, not just the per-printer one.

    An un-routed auto row on a deleted file could never be routed, and nothing
    used to tell it. ``failed`` rather than ``cancelled`` because that is the
    status the auto panel shows (``?status=pending,failed``) and the one
    ``filament_intake.fail_auto_source`` already uses for an unusable source.
    """
    from backend.app.services.library_trash import library_trash_service

    file = await a_library_file(db_session, tmp_path)
    row = await an_auto_job(db_session, library_file_id=file.id)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    row = await reread(db_session, row)
    assert row.status == "failed"
    assert row.waiting_reason == "Source file deleted"


async def test_trashing_a_library_file_leaves_a_ready_auto_row_alone(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    from backend.app.services.library_trash import library_trash_service

    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    row = await an_auto_job(db_session, blob=blob, library_file_id=file.id)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    row = await reread(db_session, row)
    assert row.status == "pending"
    assert row.queue_source_id == blob.id
    await assert_still_owned(sessions, blob, clockface)


async def test_purging_an_external_file_no_longer_deletes_the_work(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """⚠️ The P0 map's "most likely to be missed".

    An external file is purged outright — there is nothing to restore — and this
    branch used to **delete** every queue row that named it. An SMB share going
    away is exactly what the spool exists to survive, so the row stays: unhooked,
    still pending, still owning its bytes.
    """
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="A3")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path, external=True)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    await db_session.commit()

    trashed = await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    assert trashed is False, "an external file is still purged, not trashed"
    job = await reread(db_session, job)
    assert job is not None, "the work was deleted with its source"
    assert job.status == "pending"
    assert job.library_file_id is None, "the file is gone — SQLite honours no SET NULL, so the detach is code"
    assert job.queue_source_id == blob.id
    await assert_still_owned(sessions, blob, clockface)


async def test_purging_an_external_file_cancels_a_legacy_job_instead_of_deleting_it(
    db_session, tmp_path, printer_factory, sessions
):
    """Same branch, the job with nothing of its own: cancelled and visible, which
    is what the managed branch has always done (#2819's second face)."""
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="A4")
    queue = await a_queue(db_session, printer.id)
    file = await a_library_file(db_session, tmp_path, external=True)
    job = await a_job(db_session, queue_id=queue.id, library_file_id=file.id)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    job = await reread(db_session, job)
    assert job is not None
    assert job.status == "cancelled"
    assert job.waiting_reason == "Source file deleted"
    assert job.library_file_id is None


async def test_the_retention_sweeper_unhooks_a_ready_job_and_keeps_it(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="A5")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    file.deleted_at = datetime.now(timezone.utc) - timedelta(days=99)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    auto = await an_auto_job(db_session, blob=blob, library_file_id=file.id)
    await db_session.commit()

    await library_trash_service._sweep(db_session)

    job = await reread(db_session, job)
    auto = await reread(db_session, auto)
    assert (job.status, job.library_file_id, job.queue_source_id) == ("pending", None, blob.id)
    assert (auto.status, auto.library_file_id, auto.queue_source_id) == ("pending", None, blob.id)
    await assert_still_owned(sessions, blob, clockface)


async def test_hard_deleting_a_trashed_file_unhooks_both_tiers(db_session, tmp_path, printer_factory, sessions):
    """``hard_delete_now`` unhooked archives and forgot the queue entirely, so a
    row was left naming a library file that no longer existed."""
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="A6")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    file.deleted_at = datetime.now(timezone.utc)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    auto = await an_auto_job(db_session, blob=blob, library_file_id=file.id)
    await db_session.commit()

    await library_trash_service.hard_delete_now(db_session, file)

    job = await reread(db_session, job)
    auto = await reread(db_session, auto)
    assert job is not None and job.library_file_id is None
    assert auto is not None and auto.library_file_id is None


async def test_the_scanner_removing_a_vanished_file_keeps_the_work(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """A share that lost a file: the row is unhooked, not deleted, and a job with
    its own copy is not even cancelled."""
    from backend.app.services import library_scan

    printer = await printer_factory(name="A7")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path, name="vanished.gcode.3mf")
    ready = await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    legacy = await a_job(db_session, queue_id=queue.id, library_file_id=file.id, position=2)
    await db_session.commit()
    Path(file.file_path).unlink()

    known = {
        file.file_path: library_scan._Known(id=file.id, file_hash=None, file_size=1, fs_modified_at=None),
    }
    counters = dict.fromkeys(("files_removed", "folders_removed"), 0)
    refused = await library_scan.remove_vanished({"other.3mf"}, known, {"": 1}, counters)

    assert refused is False and counters["files_removed"] == 1
    ready = await reread(db_session, ready)
    legacy = await reread(db_session, legacy)
    assert (ready.status, ready.library_file_id, ready.queue_source_id) == ("pending", None, blob.id)
    assert legacy.status == "cancelled", "a job with no copy of its own can never print this file again"
    assert legacy.library_file_id is None
    await assert_still_owned(sessions, blob, clockface)


async def test_trashing_an_archive_leaves_a_ready_job_alone(db_session, tmp_path, printer_factory, sessions, clockface):
    from backend.app.services.archive_purge import archive_purge_service

    printer = await printer_factory(name="A8")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(
        db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, project_id=None, project_line_id=None
    )
    await db_session.commit()

    await archive_purge_service.move_to_trash(db_session, archive)

    job = await reread(db_session, job)
    assert job.status == "pending"
    assert job.queue_source_id == blob.id
    assert job.archive_id == archive.id, "a trashed archive is restorable — the link stays"
    await assert_still_owned(sessions, blob, clockface)


async def test_trashing_an_archive_reaches_the_auto_tier_too(db_session, tmp_path, printer_factory, sessions):
    """``archive_purge`` cancelled ``print_queue`` rows only, so a pending auto row
    on a trashed archive was told nothing at all."""
    from backend.app.services.archive_purge import archive_purge_service

    printer = await printer_factory(name="A9")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    row = await an_auto_job(db_session, archive_id=archive.id)
    await db_session.commit()

    await archive_purge_service.move_to_trash(db_session, archive)

    row = await reread(db_session, row)
    assert row.status == "failed"
    assert row.waiting_reason == "Source archive deleted"


async def test_hard_deleting_an_archive_unhooks_the_work_on_both_tiers(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """⚠️ The one cascade that destroyed a job because its source went away.

    ``print_queue.archive_id`` was ``ON DELETE CASCADE`` until m173 — on
    PostgreSQL the row went with the archive, on SQLite the id was left dangling
    because no FK rule is ever enforced. Now the link nulls, in code, and the work
    stands.
    """
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory(name="B1")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id)
    auto = await an_auto_job(db_session, blob=blob, archive_id=archive.id)
    await db_session.commit()

    assert await ArchiveService(db_session).delete_archive(archive.id) is True

    job = await reread(db_session, job)
    auto = await reread(db_session, auto)
    assert job is not None and job.archive_id is None and job.queue_source_id == blob.id
    assert auto is not None and auto.archive_id is None and auto.queue_source_id == blob.id
    await assert_still_owned(sessions, blob, clockface)


async def test_hard_deleting_an_archive_cancels_a_legacy_job_that_never_saw_the_trash(
    db_session, tmp_path, printer_factory, sessions
):
    """Printer delete hard-deletes archives without trashing them first, so the
    cancel has to live on the hard-delete path as well."""
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory(name="B2")
    queue = await a_queue(db_session, printer.id)
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, archive_id=archive.id)
    await db_session.commit()

    await ArchiveService(db_session).delete_archive(archive.id)

    job = await reread(db_session, job)
    assert job is not None
    assert job.status == "cancelled"
    assert job.waiting_reason == "Source archive deleted"
    assert job.archive_id is None


async def test_a_job_naming_another_archive_is_untouched(db_session, tmp_path, printer_factory, sessions):
    """⚠️ Scoped to the ids actually deleted. Any wider rule would disconnect live
    jobs from their own sources."""
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory(name="B3")
    queue = await a_queue(db_session, printer.id)
    doomed = await an_archive_with_its_own_bytes(db_session, printer.id, name="doomed.gcode.3mf")
    keeper = await an_archive_with_its_own_bytes(db_session, printer.id, name="keeper.gcode.3mf")
    other = await a_job(db_session, queue_id=queue.id, archive_id=keeper.id)
    await db_session.commit()

    await ArchiveService(db_session).delete_archive(doomed.id)

    other = await reread(db_session, other)
    assert other.archive_id == keeper.id
    assert other.status == "pending"


# --------------------------------------------------------------------------- #
# §9 — every release path frees the reference exactly once
# --------------------------------------------------------------------------- #


async def test_deleting_a_queue_row_frees_the_reference(db_session, tmp_path, printer_factory, sessions, clockface):
    from backend.app.api.routes.print_queue import delete_queue_item

    printer = await printer_factory(name="C1")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    job = await a_job(db_session, queue_id=queue.id, blob=blob)
    await db_session.commit()

    await delete_queue_item(item_id=job.id, db=db_session, auth_result=(None, True))

    await assert_released(sessions, blob, clockface)


async def test_deleting_a_queue_row_takes_its_router_half_with_it(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """Both halves of an assigned job hold the blob, and ONE delete releases both:
    ``detach_print_queue_refs`` removes the router row the per-printer item was
    promoted from (§9 — "assigned half прибирається разом із printer item")."""
    from backend.app.api.routes.print_queue import delete_queue_item

    printer = await printer_factory(name="C2")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    router = await an_auto_job(db_session, blob=blob, status="assigned")
    job = await a_job(db_session, queue_id=queue.id, blob=blob, source_auto_item_id=router.id)
    router.assigned_to_item_id = job.id
    await db_session.commit()

    await delete_queue_item(item_id=job.id, db=db_session, auth_result=(None, True))

    db_session.expunge_all()
    assert await db_session.get(AutoQueueItem, router.id) is None, "the router half outlived its printer item"
    await assert_released(sessions, blob, clockface)


async def test_cancelling_an_unrouted_auto_row_frees_the_reference(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    from backend.app.api.routes.auto_queue import cancel_auto_queue_item

    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    row = await an_auto_job(db_session, blob=blob)
    await db_session.commit()

    await cancel_auto_queue_item(item_id=row.id, db=db_session, _=None)

    await assert_released(sessions, blob, clockface)


async def test_a_batch_cancel_frees_every_unrouted_reference(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    from backend.app.api.routes.auto_queue import cancel_auto_queue_batch

    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    for _ in range(3):
        await an_auto_job(db_session, blob=blob, batch_id="batch-7")
    await db_session.commit()

    await cancel_auto_queue_batch(batch_id="batch-7", db=db_session, _=None)

    await assert_released(sessions, blob, clockface)


async def test_deleting_a_printer_frees_the_references_of_every_row_it_took(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """⚠️ The printer-delete route bulk-deletes its queue rows, bypassing the one
    choke point — so the router half of every assigned job survived, holding the
    blob for ever with nothing left able to reach it."""
    from backend.app.api.routes.printers import delete_printer

    printer = await printer_factory(name="C4")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    router = await an_auto_job(db_session, blob=blob, status="assigned")
    job = await a_job(db_session, queue_id=queue.id, blob=blob, source_auto_item_id=router.id)
    router.assigned_to_item_id = job.id
    await db_session.commit()

    await delete_printer(printer_id=printer.id, delete_archives=True, _=None, db=db_session)

    db_session.expunge_all()
    assert await db_session.get(AutoQueueItem, router.id) is None
    await assert_released(sessions, blob, clockface)


async def test_clearing_the_plate_frees_the_reference(db_session, tmp_path, printer_factory, sessions, clockface):
    from backend.app.services.plate_hold import answer_by_clearing

    printer = await printer_factory(name="C5", require_plate_clear=True)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    await a_job(
        db_session,
        queue_id=queue.id,
        blob=blob,
        archive_id=archive.id,
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    assert await answer_by_clearing(db_session, printer.id) == 1

    await assert_released(sessions, blob, clockface)


async def test_clearing_the_plate_still_answers_when_the_archive_bytes_are_gone(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """⚠️ Clear plate must never fail. It is the operator's explicit answer, it is
    where the plate gate is released, and a refusal would leave a printer waiting
    for a question that can no longer be answered. The lost bytes are reported in
    the log, not by refusing the person standing at the machine."""
    from backend.app.services.plate_hold import answer_by_clearing

    printer = await printer_factory(name="C6", require_plate_clear=True)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    await a_job(
        db_session,
        queue_id=queue.id,
        blob=blob,
        archive_id=archive.id,
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    await db_session.commit()
    (settings.base_dir / archive.file_path).unlink()

    assert await answer_by_clearing(db_session, printer.id) == 1
    await assert_released(sessions, blob, clockface)


async def test_repeating_keeps_the_reference(db_session, tmp_path, printer_factory, sessions, clockface):
    """Repeat re-arms the same row, so the blob it prints is the blob it had."""
    from backend.app.services.plate_hold import answer_by_repeating

    printer = await printer_factory(name="C7", require_plate_clear=True)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(
        db_session,
        queue_id=queue.id,
        blob=blob,
        archive_id=archive.id,
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    rearmed = await answer_by_repeating(db_session, printer.id)

    assert rearmed is not None and rearmed.id == job.id
    assert rearmed.status == "pending"
    assert rearmed.queue_source_id == blob.id
    await assert_still_owned(sessions, blob, clockface)


async def test_a_printing_row_closed_on_age_keeps_its_reference(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """Spec §9's row added 2026-09-13, driven through the real closer.

    ``_close_stale_printing_rows`` ends an archive that has been ``printing`` past
    its predicted end with no completion event. That is the tidying of a stuck
    row, not a print ending: nobody has answered for that plate, so the queue row
    and its bytes stay exactly where they are.
    """
    import logging

    from backend.app.main import _close_stale_printing_rows

    printer = await printer_factory(name="C8")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id, name="stale.gcode.3mf")
    archive.status = "printing"
    archive.started_at = datetime.now(timezone.utc) - timedelta(hours=9)
    archive.print_time_seconds = 60
    archive.completed_at = None
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="printing")
    await db_session.commit()

    await _close_stale_printing_rows(printer.id, "something-else.3mf", db_session, logging.getLogger("test"))

    db_session.expunge_all()
    closed = await db_session.get(PrintArchive, archive.id)
    assert closed.status == "completed", "the closer did not run — the rest of this test proves nothing"
    job = await reread(db_session, job)
    assert job is not None and job.queue_source_id == blob.id
    await assert_still_owned(sessions, blob, clockface)


# --------------------------------------------------------------------------- #
# §9 — completion confirms independent archive bytes (A18)
# --------------------------------------------------------------------------- #


async def test_completion_tidies_the_row_once_the_archive_holds_its_own_bytes(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D1", require_plate_clear=False)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="completed")
    await db_session.commit()

    deleted = await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False)

    assert deleted is True
    await assert_released(sessions, blob, clockface)


async def test_completion_keeps_the_row_when_the_archive_has_no_bytes_of_its_own(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """§9: do not lose the last bytes.

    The archive is the independent copy this row's blob is allowed to be released
    against. With no such copy the automatic cleanup keeps the row, its reference
    and a visible diagnostic, and the archive pipeline retries — the row is not
    quietly deleted and the bytes are not quietly collected.
    """
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D2", require_plate_clear=False)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="completed")
    await db_session.commit()
    (settings.base_dir / archive.file_path).unlink()

    deleted = await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False)

    assert deleted is False
    job = await reread(db_session, job)
    assert job is not None and job.queue_source_id == blob.id
    assert job.waiting_reason, "the operator is owed a reason for a row that stayed"
    await assert_still_owned(sessions, blob, clockface)


async def test_completion_never_creates_a_second_archive(db_session, tmp_path, printer_factory, sessions):
    """A18: one archive per physical print, whatever the cleanup decides."""
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D3", require_plate_clear=False)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="completed")
    await db_session.commit()
    (settings.base_dir / archive.file_path).unlink()

    before = await db_session.scalar(select(func.count()).select_from(PrintArchive))
    await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False)
    db_session.expunge_all()
    after = await db_session.scalar(select(func.count()).select_from(PrintArchive))

    assert after == before == 1


async def test_a_retried_download_lets_the_held_row_go(db_session, tmp_path, printer_factory, sessions, clockface):
    """The held row is not held for ever: the archive pipeline is what unblocks it.

    Once the 3MF is attached the very same call tidies the row, which is why the
    diagnostic says "retrying" rather than "gone".
    """
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D4", require_plate_clear=False)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="completed")
    await db_session.commit()
    attached = settings.base_dir / archive.file_path
    attached.unlink()

    assert await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False) is False
    make_3mf(attached)
    assert await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False) is True
    await assert_released(sessions, blob, clockface)


async def test_a_legacy_completion_is_tidied_exactly_as_before(db_session, tmp_path, printer_factory, sessions):
    """⚠️ The confirmation gates ONLY a row that owns captured bytes.

    A print picked up from the printer's screen is archived with ``file_path=""``
    and its 3MF may never arrive at all (P1S / A1 firmware locks the file). Such a
    row has no bytes to lose, and holding it would leave a permanent completed row
    on every one of those prints.
    """
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D5", require_plate_clear=False)
    queue = await a_queue(db_session, printer.id)
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    archive.file_path = ""
    job = await a_job(db_session, queue_id=queue.id, archive_id=archive.id, status="completed")
    await db_session.commit()

    assert await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False) is True


async def test_an_archive_that_points_into_the_spool_is_not_an_independent_copy(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """ "Independent" is the load-bearing word. An archive whose ``file_path`` is
    the spool object itself is the same bytes, so releasing the blob against it
    would delete the file the archive is supposed to keep."""
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D6", require_plate_clear=False)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    archive.file_path = blob.relative_path
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="completed")
    await db_session.commit()

    assert await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False) is False
    await assert_still_owned(sessions, blob, clockface)


async def test_a_held_plate_still_wins_over_the_confirmation(db_session, tmp_path, printer_factory, sessions):
    """The plate question is asked first and is unchanged: a printer that confirms
    its plate keeps the row whatever the archive looks like, so the confirmation
    cannot turn a held row into a deleted one."""
    from backend.app.services.plate_hold import clean_up_finished_row

    printer = await printer_factory(name="D7", require_plate_clear=True)
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    job = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, status="completed")
    await db_session.commit()

    assert await clean_up_finished_row(db_session, job, queue_status="completed", plate_auto_cleared=False) is False
    job = await reread(db_session, job)
    assert job.waiting_reason is None, "a plate hold is not a diagnostic"


# --------------------------------------------------------------------------- #
# §9 — a broken blob keeps its reference while a job names it
# --------------------------------------------------------------------------- #


async def test_a_broken_blob_is_not_evicted_while_a_job_still_names_it(
    db_session, tmp_path, printer_factory, sessions, clockface
):
    """That reference is how the operator sees which jobs died with those bytes."""
    printer = await printer_factory(name="E1")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    await a_job(db_session, queue_id=queue.id, blob=blob)
    async with sessions() as session:
        row = await session.get(QueueSource, blob.id)
        row.state = STATE_BROKEN
        await session.commit()
    await db_session.commit()

    report = await sweep_twice(sessions, clockface)

    assert report.released == 0
    async with sessions() as session:
        assert (await session.get(QueueSource, blob.id)).state == STATE_BROKEN


# --------------------------------------------------------------------------- #
# §10 — the delete pre-flight
# --------------------------------------------------------------------------- #


async def test_the_pre_flight_splits_self_contained_from_dependent(db_session, tmp_path, printer_factory, sessions):
    from backend.app.services.queue_source_release import delete_impact

    printer = await printer_factory(name="F1")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id, position=2)
    await a_job(db_session, queue_id=queue.id, library_file_id=file.id, position=3)
    await db_session.commit()

    impact = await delete_impact(db_session, library_file_ids=[file.id])

    assert (impact.pending_total, impact.self_contained, impact.needs_original) == (3, 2, 1)


async def test_the_pre_flight_counts_pending_rows_only(db_session, tmp_path, printer_factory, sessions):
    """⚠️ A terminal row neither prints nor gets cancelled, so counting it makes
    the dialog announce rows that do not exist."""
    from backend.app.services.queue_source_release import delete_impact

    printer = await printer_factory(name="F2")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    for index, status in enumerate(("completed", "cancelled", "failed", "skipped", "printing")):
        await a_job(
            db_session, queue_id=queue.id, blob=blob, library_file_id=file.id, status=status, position=index + 1
        )
    await db_session.commit()

    impact = await delete_impact(db_session, library_file_ids=[file.id])

    assert (impact.pending_total, impact.self_contained, impact.needs_original) == (0, 0, 0)


async def test_the_pre_flight_counts_both_tiers(db_session, tmp_path, printer_factory, sessions):
    from backend.app.services.queue_source_release import delete_impact

    printer = await printer_factory(name="F3")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id)
    await an_auto_job(db_session, blob=blob, archive_id=archive.id)
    await an_auto_job(db_session, archive_id=archive.id)
    await db_session.commit()

    impact = await delete_impact(db_session, archive_ids=[archive.id])

    assert (impact.pending_total, impact.self_contained, impact.needs_original) == (3, 2, 1)


async def test_an_assigned_auto_row_is_not_counted_twice(db_session, tmp_path, printer_factory, sessions):
    """⚠️ An assigned router row and its per-printer twin are ONE piece of work.
    Counting both would tell the operator two prints are affected where there is
    one, and the number in that sentence is the whole point of it."""
    from backend.app.services.queue_source_release import delete_impact

    printer = await printer_factory(name="F4")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    router = await an_auto_job(db_session, blob=blob, archive_id=archive.id, status="assigned")
    twin = await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id, source_auto_item_id=router.id)
    router.assigned_to_item_id = twin.id
    await db_session.commit()

    impact = await delete_impact(db_session, archive_ids=[archive.id])

    assert (impact.pending_total, impact.self_contained) == (1, 1)


@pytest.mark.parametrize("state", [STATE_BROKEN, STATE_DELETING])
async def test_a_blob_that_is_not_ready_does_not_make_a_job_self_contained(
    db_session, tmp_path, printer_factory, sessions, state
):
    """⚠️ The verdict per row is ``source_storage_state`` — the same function that
    fills the API field the UI reads — and not "has a ``queue_source_id``". Only
    ``ready`` is a promise that the bytes are there; a broken or being-collected
    blob means this job dies with its original like any legacy one.
    """
    from backend.app.services.queue_source_release import delete_impact

    printer = await printer_factory(name="F5")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    async with sessions() as session:
        row = await session.get(QueueSource, blob.id)
        row.state = state
        await session.commit()
    await db_session.commit()

    impact = await delete_impact(db_session, library_file_ids=[file.id])

    assert (impact.pending_total, impact.self_contained, impact.needs_original) == (1, 0, 1)


async def test_the_pre_flight_agrees_with_what_the_delete_then_does(db_session, tmp_path, printer_factory, sessions):
    """The number on the confirmation and the outcome of pressing it are the same
    answer, which is the only reason the sentence is worth showing."""
    from backend.app.services.library_trash import library_trash_service
    from backend.app.services.queue_source_release import delete_impact

    printer = await printer_factory(name="F6")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    kept = await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    dying = await a_job(db_session, queue_id=queue.id, library_file_id=file.id, position=2)
    await db_session.commit()

    impact = await delete_impact(db_session, library_file_ids=[file.id])
    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    kept = await reread(db_session, kept)
    dying = await reread(db_session, dying)
    assert (impact.self_contained, impact.needs_original) == (1, 1)
    assert kept.status == "pending"
    assert dying.status == "cancelled"


async def test_the_archive_pre_flight_endpoint_answers_both_counts(
    async_client, db_session, tmp_path, printer_factory, sessions
):
    printer = await printer_factory(name="F7")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    archive = await an_archive_with_its_own_bytes(db_session, printer.id)
    await a_job(db_session, queue_id=queue.id, blob=blob, archive_id=archive.id)
    await a_job(db_session, queue_id=queue.id, archive_id=archive.id, position=2)
    await a_job(db_session, queue_id=queue.id, archive_id=archive.id, position=3, status="completed")
    await db_session.commit()

    body = (await async_client.get(f"/api/v1/archives/{archive.id}/delete-impact")).json()

    assert body["related_queue_items"] == 3, "the pre-existing all-status count is unchanged"
    assert body["currently_printing"] == 0
    assert body["pending_queue_items"] == 2
    assert body["pending_self_contained"] == 1
    assert body["pending_needs_original"] == 1


async def test_the_library_file_pre_flight_endpoint_answers_the_same_shape(
    async_client, db_session, tmp_path, printer_factory, sessions
):
    """A library file had no pre-flight at all, so its confirmation had no number
    it could honestly put in a sentence."""
    printer = await printer_factory(name="F8")
    queue = await a_queue(db_session, printer.id)
    blob = await a_ready_blob(sessions, tmp_path / "share" / "lamp.gcode.3mf")
    file = await a_library_file(db_session, tmp_path)
    await a_job(db_session, queue_id=queue.id, blob=blob, library_file_id=file.id)
    await an_auto_job(db_session, library_file_id=file.id)
    await db_session.commit()

    response = await async_client.get(f"/api/v1/library/files/{file.id}/delete-impact")

    assert response.status_code == 200
    body = response.json()
    assert body["pending_queue_items"] == 2
    assert body["pending_self_contained"] == 1
    assert body["pending_needs_original"] == 1
    assert body["currently_printing"] == 0


async def test_the_library_file_pre_flight_reports_the_blocker_that_refuses_the_delete(
    async_client, db_session, tmp_path, printer_factory, sessions
):
    """The route refuses with 409 while a row is printing; the dialog is allowed to
    know that before the operator presses the button."""
    printer = await printer_factory(name="F9")
    queue = await a_queue(db_session, printer.id)
    file = await a_library_file(db_session, tmp_path)
    await a_job(db_session, queue_id=queue.id, library_file_id=file.id, status="printing")
    await db_session.commit()

    body = (await async_client.get(f"/api/v1/library/files/{file.id}/delete-impact")).json()

    assert body["currently_printing"] == 1


async def test_the_library_file_pre_flight_404s_for_a_file_that_is_not_there(async_client):
    assert (await async_client.get("/api/v1/library/files/424242/delete-impact")).status_code == 404
