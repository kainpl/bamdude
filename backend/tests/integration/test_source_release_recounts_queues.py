"""A source release that cancels queued jobs recounts their printers' queues — after the commit.

When an original goes away (a library file trashed or purged, a vanished share, an
archive's hard delete), ``queue_source_release`` cancels the pending printer rows that
cannot print without it. Those rows were still counted in the queue's cached
``pending_count``, so the printer card kept announcing work that would never run until
some other writer happened to touch that queue.

The recount cannot run where the cancel runs — that is inside a deletion's guarded
phase, which owns the writer and must stay short. So the release stages the queues on
the session; only the ROOT commit keeps them; and the deletion's owner, once its commit
returned and its plan ended, calls ``recount_released_queues``, which writes them in a
transaction of its own. A rollback recounts nothing, and a recount that fails is logged,
never the deletion's error.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_counters, queue_source_release

# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _make_3mf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", b"<model/>")
    return path


async def _queue(db, printer_id: int, *, pending_count: int) -> PrinterQueue:
    """The printer's own queue (its id IS the printer's), with the cached count other writers left."""
    queue = await db.get(PrinterQueue, printer_id)
    if queue is None:
        queue = PrinterQueue(id=printer_id, printer_id=printer_id, status="idle")
        db.add(queue)
    queue.pending_count = pending_count
    await db.flush()
    return queue


async def _job(
    db, queue_id: int, *, library_file_id: int | None = None, archive_id: int | None = None, position: int = 1
) -> PrintQueueItem:
    """A pending job with no copy of its own — the kind a release cancels."""
    item = PrintQueueItem(
        queue_id=queue_id,
        status="pending",
        library_file_id=library_file_id,
        archive_id=archive_id,
        position=position,
    )
    db.add(item)
    await db.flush()
    return item


async def _file(db, tmp_path: Path, *, name: str = "part.gcode.3mf", external: bool = False) -> LibraryFile:
    path = _make_3mf(tmp_path / "share" / name)
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


async def _archive(db, printer_id: int) -> PrintArchive:
    """An archive whose 3MF is in an archive folder of its own, two levels deep."""
    path = _make_3mf(settings.archive_dir / "release-recount" / uuid.uuid4().hex / "shelf.gcode.3mf")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = PrintArchive(
        printer_id=printer_id,
        filename=path.name,
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


async def _counts(db, queue_id: int) -> tuple[int, int]:
    """``(cached pending_count, pending rows in the database)``."""
    cached = await db.scalar(select(PrinterQueue.pending_count).where(PrinterQueue.id == queue_id))
    actual = await db.scalar(
        select(func.count())
        .select_from(PrintQueueItem)
        .where(PrintQueueItem.queue_id == queue_id, PrintQueueItem.status == "pending")
    )
    return cached, actual


def _recorded(monkeypatch) -> list[int]:
    """Record every queue the recount writes, still writing it for real."""
    calls: list[int] = []
    real = queue_counters.update_queue_counters

    async def recording(db, queue_id):
        calls.append(queue_id)
        await real(db, queue_id)

    monkeypatch.setattr(queue_counters, "update_queue_counters", recording)
    return calls


# --------------------------------------------------------------------------- #
# The release's hand-off
# --------------------------------------------------------------------------- #


async def test_a_release_hands_back_the_printer_queues_it_cancelled_on(db_session, tmp_path, printer_factory):
    first = await printer_factory(name="R1")
    second = await printer_factory(name="R2")
    doomed = await _file(db_session, tmp_path, name="doomed.gcode.3mf")
    keeper = await _file(db_session, tmp_path, name="keeper.gcode.3mf")
    q1 = await _queue(db_session, first.id, pending_count=1)
    q2 = await _queue(db_session, second.id, pending_count=1)
    await _job(db_session, q1.id, library_file_id=doomed.id)
    await _job(db_session, q2.id, library_file_id=keeper.id)
    db_session.add(AutoQueueItem(status="pending", library_file_id=doomed.id, position=1))
    await db_session.commit()

    released = await queue_source_release.source_trashed(
        db_session, library_file_ids=[doomed.id], reason=queue_source_release.REASON_FILE_DELETED
    )

    # The auto row is cancelled too, but the distributor keeps no cached counter to fix.
    assert released.cancelled == 2
    assert released.queue_ids == frozenset({q1.id}), "only the queue that lost a pending row is stale"
    await db_session.rollback()


async def test_a_rolled_back_release_recounts_nothing(db_session, tmp_path, printer_factory, monkeypatch):
    printer = await printer_factory(name="R3")
    file = await _file(db_session, tmp_path)
    queue = await _queue(db_session, printer.id, pending_count=1)
    await _job(db_session, queue.id, library_file_id=file.id)
    queue_id, file_id = queue.id, file.id
    await db_session.commit()
    calls = _recorded(monkeypatch)

    await queue_source_release.source_trashed(db_session, library_file_ids=[file_id], reason="test")
    await db_session.rollback()
    await queue_source_release.recount_released_queues(db_session)

    assert calls == [], "nothing was committed, so nothing is recounted"
    assert await _counts(db_session, queue_id) == (1, 1)


async def test_a_released_savepoint_waits_for_the_root_commit(db_session, tmp_path, printer_factory, monkeypatch):
    """``after_commit`` also fires for a savepoint's RELEASE; that is not yet a fact."""
    printer = await printer_factory(name="R4")
    file = await _file(db_session, tmp_path)
    queue = await _queue(db_session, printer.id, pending_count=1)
    await _job(db_session, queue.id, library_file_id=file.id)
    await db_session.commit()
    calls = _recorded(monkeypatch)

    async with db_session.begin_nested():
        await queue_source_release.source_trashed(db_session, library_file_ids=[file.id], reason="test")
    await queue_source_release.recount_released_queues(db_session)
    assert calls == [], "the root transaction is still open"

    await db_session.commit()
    await queue_source_release.recount_released_queues(db_session)
    assert calls == [queue.id]
    assert await _counts(db_session, queue.id) == (0, 0)


async def test_a_committed_release_is_recounted_once(db_session, tmp_path, printer_factory, monkeypatch):
    printer = await printer_factory(name="R5")
    file = await _file(db_session, tmp_path)
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, library_file_id=file.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    await db_session.commit()
    calls = _recorded(monkeypatch)

    await queue_source_release.source_trashed(db_session, library_file_ids=[file.id], reason="test")
    await db_session.commit()
    await queue_source_release.recount_released_queues(db_session)
    await queue_source_release.recount_released_queues(db_session)

    assert calls == [queue.id], "a second owner on the same session finds nothing left to do"
    assert await _counts(db_session, queue.id) == (1, 1)


async def test_a_failing_recount_is_logged_and_never_raised(db_session, tmp_path, printer_factory, monkeypatch, caplog):
    printer = await printer_factory(name="R6")
    file = await _file(db_session, tmp_path)
    queue = await _queue(db_session, printer.id, pending_count=1)
    await _job(db_session, queue.id, library_file_id=file.id)
    await db_session.commit()

    async def broken(db, queue_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(queue_counters, "update_queue_counters", broken)
    await queue_source_release.source_trashed(db_session, library_file_ids=[file.id], reason="test")
    await db_session.commit()
    with caplog.at_level(logging.WARNING, logger=queue_source_release.__name__):
        await queue_source_release.recount_released_queues(db_session)

    assert any("not recounted" in record.getMessage() for record in caplog.records)
    # The deletion stands; the cache waits for the queue's next writer.
    assert await _counts(db_session, queue.id) == (1, 0)


# --------------------------------------------------------------------------- #
# Every owner that commits a release recounts after it
# --------------------------------------------------------------------------- #


async def test_hard_deleting_a_library_file_recounts_its_jobs_queue(db_session, tmp_path, printer_factory):
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="L1")
    file = await _file(db_session, tmp_path, external=True)
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, library_file_id=file.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    await db_session.commit()

    await library_trash_service.hard_delete_now(db_session, file)

    assert await _counts(db_session, queue.id) == (1, 1)


async def test_the_retention_sweeper_recounts_the_queues_it_cancelled_on(db_session, tmp_path, printer_factory):
    from backend.app.services.library_trash import library_trash_service

    printer = await printer_factory(name="L2")
    file = await _file(db_session, tmp_path)
    file.deleted_at = datetime.now(timezone.utc) - timedelta(days=400)
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, library_file_id=file.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    await db_session.commit()

    assert await library_trash_service._sweep(db_session) == 1

    assert await _counts(db_session, queue.id) == (1, 1)


async def test_the_scanner_purging_a_vanished_file_recounts_its_jobs_queue(db_session, tmp_path, printer_factory):
    from backend.app.services import library_scan

    printer = await printer_factory(name="L3")
    file = await _file(db_session, tmp_path, name="vanished.gcode.3mf", external=True)
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, library_file_id=file.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    await db_session.commit()
    Path(file.file_path).unlink()

    known = {file.file_path: library_scan._Known(id=file.id, file_hash=None, file_size=1, fs_modified_at=None)}
    counters = dict.fromkeys(("files_removed", "folders_removed"), 0)
    refused = await library_scan.remove_vanished({"other.3mf"}, known, {"": 1}, counters)

    assert refused is False and counters["files_removed"] == 1
    assert await _counts(db_session, queue.id) == (1, 1)


async def test_hard_deleting_a_trashed_archive_recounts_its_jobs_queue(db_session, printer_factory):
    from backend.app.services.archive_purge import archive_purge_service

    printer = await printer_factory(name="A1")
    archive = await _archive(db_session, printer.id)
    archive.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, archive_id=archive.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    archive_id = archive.id
    await db_session.commit()

    assert await archive_purge_service.hard_delete_now(archive_id) is True

    assert await _counts(db_session, queue.id) == (1, 1)


async def test_the_library_cleanup_step_recounts_a_plain_trash(db_session, tmp_path, printer_factory):
    """Every library door calls ``library_delete_cleanup.finish`` after its commit; a trash stages no removal."""
    from backend.app.services import library_delete_cleanup

    printer = await printer_factory(name="L4")
    file = await _file(db_session, tmp_path, name="trashed.gcode.3mf")
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, library_file_id=file.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    await db_session.commit()

    await queue_source_release.source_trashed(db_session, library_file_ids=[file.id], reason="test")
    await db_session.commit()
    await library_delete_cleanup.finish(db_session)

    assert await _counts(db_session, queue.id) == (1, 1)


async def test_trashing_an_archive_recounts_its_jobs_queue(async_client, db_session, printer_factory):
    printer = await printer_factory(name="A2")
    archive = await _archive(db_session, printer.id)
    queue = await _queue(db_session, printer.id, pending_count=2)
    await _job(db_session, queue.id, archive_id=archive.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    archive_id = archive.id
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/archives/{archive_id}")

    assert response.status_code == 200, response.text
    assert response.json()["trashed"] is True
    assert await _counts(db_session, queue.id) == (1, 1)


async def test_the_archive_hard_delete_recounts_for_every_caller(db_session, printer_factory):
    """Printer deletion calls it in a loop; the recount follows each archive's own commit."""
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory(name="A3")
    other = await printer_factory(name="A4")
    archive = await _archive(db_session, printer.id)
    queue = await _queue(db_session, other.id, pending_count=2)
    await _job(db_session, queue.id, archive_id=archive.id)
    await _job(db_session, queue.id, library_file_id=None, position=2)
    archive_id = archive.id
    await db_session.commit()

    assert await ArchiveService(db_session).delete_archive(archive_id) is True

    assert await _counts(db_session, queue.id) == (1, 1)
