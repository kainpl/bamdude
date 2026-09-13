"""One add request copies its source ONCE and enqueues from the copy — spec §5.

Spec: ``60-specs/queue-source-spool-spec.md`` §5 (the seven steps and the
fan-out rule) and A01. Everything here is about the three main producers —
``services/queue_add.py``, ``auto_queue_add.py`` and ``queue_batch.py`` — and the
four properties the fan-out rule turns into:

* **one capture per unique source per request**, however many plates, copies,
  printers or quantity come out of it (the read of the ORIGINAL is counted, not
  the number of blobs — a second copy that deduplicates still walked the share);
* **the long DB transaction is released before the copy** (§5 step 1);
* **requirements and the resolved plate come from the captured bytes** (step 4),
  so a request naming a plate the file does not have refuses the whole add and
  leaves nothing behind;
* **a direct print does not hold the printer for the length of the copy** and
  re-checks availability before it takes the real claim.

No test here waits for a real deadline: a slow share is a ``threading.Event``
inside the worker's ``open``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.queue_add import add_items_to_printer_queue
from backend.app.services.queue_batch import enqueue_batch_copies
from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


PLATE_FILAMENTS = [{"id": 1, "type": "PLA", "color": "#FF0000", "used_g": "5"}]


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
    """Publication owns its own transaction, so it needs the TEST engine's factory.

    ``queue_sources.publish`` reads ``database.async_session`` at call time (the
    ``client`` fixtures patch the same attribute), so patching it here is what
    makes a service-level test publish into the same database its assertions
    read.
    """
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


@pytest.fixture
def reads(monkeypatch):
    """Every read of an ORIGINAL file, in order — one per capture.

    The spool deduplicates by content, so counting ``queue_sources`` rows cannot
    tell one capture from five that happened to agree. What §5's fan-out rule is
    about is the walk over the share, and that is exactly what this counts.
    """
    seen: list[Path] = []
    real = queue_sources._open_source

    def spy(path: Path):
        seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(queue_sources, "_open_source", spy)
    return seen


async def a_source(db, tmp_path, *, name="part.gcode.3mf", plates=None) -> LibraryFile:
    """A sliced library file on disk, committed and idle."""
    path = write_routing_3mf(tmp_path / name, plates or {15: PLATE_FILAMENTS})
    row = LibraryFile(filename=path.name, file_path=str(path), file_size=path.stat().st_size, file_type="gcode")
    db.add(row)
    await db.commit()
    return row


def object_of(source: QueueSource) -> Path:
    return Path(settings.base_dir) / source.relative_path


async def blobs(db) -> list[QueueSource]:
    return list((await db.execute(select(QueueSource).order_by(QueueSource.id))).scalars())


def staging_litter() -> list[Path]:
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.is_dir() else []


# --------------------------------------------------------------------------- #
# A01 — one capture, whatever the fan-out
# --------------------------------------------------------------------------- #


async def test_one_capture_serves_every_plate_and_copy_of_one_auto_queue_request(db_session, tmp_path, sessions, reads):
    """Two plates × three copies = six rows, one read of the share, one blob."""
    source = await a_source(db_session, tmp_path, plates={1: PLATE_FILAMENTS, 2: PLATE_FILAMENTS})
    original = Path(source.file_path)

    items = await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=source.id, plate_ids=[1, 2], quantity=3),
        None,
    )

    assert len(items) == 6
    assert reads == [original], "the fan-out must capture once, never once per plate or per copy"
    rows = await blobs(db_session)
    assert len(rows) == 1
    blob = rows[0]
    assert blob.sha256 == hashlib.sha256(original.read_bytes()).hexdigest()
    assert blob.size_bytes == original.stat().st_size
    assert object_of(blob).is_file()
    assert {item.queue_source_id for item in items} == {blob.id}
    assert sorted(item.plate_id for item in items) == [1, 1, 1, 2, 2, 2]
    assert len({item.batch_id for item in items}) == 1
    for item in items:
        assert item.source_snapshot == {
            "version": SOURCE_SNAPSHOT_VERSION,
            "provenance": {"kind": "library_file", "id": source.id},
            "display_filename": source.filename,
            "format": "3mf",
            "plate_fallback": None,
        }
    assert staging_litter() == []


async def test_the_printer_queue_fan_out_captures_once_for_every_copy(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """``quantity=5`` on one printer: five rows sharing one blob and one batch."""
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()

    items, returned_queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15, quantity=5),
        None,
    )

    assert len(reads) == 1
    assert returned_queue.id == queue.id
    rows = await blobs(db_session)
    assert len(rows) == 1
    assert {item.queue_source_id for item in items} == {rows[0].id}
    assert [item.position for item in items] == [1, 2, 3, 4, 5]
    assert {item.status for item in items} == {"pending"}
    assert all(item.source_snapshot["display_filename"] == source.filename for item in items)
    # The row is live in the caller's session, not a detached copy from the
    # publication's own transaction: the route re-queries it for the response.
    assert all(item in db_session for item in items)
    assert object_of(rows[0]).is_file()
    assert staging_litter() == []


async def test_batch_copies_capture_once_and_share_one_blob(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """The direct-print quantity path (``enqueue_batch_copies``) is one capture too."""
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()

    items, batch_id = await enqueue_batch_copies(
        db_session, printer_id=printer.id, count=3, library_file_id=source.id, plate_id=15
    )

    assert len(items) == 3 and batch_id
    assert len(reads) == 1
    rows = await blobs(db_session)
    assert len(rows) == 1
    assert {item.queue_source_id for item in items} == {rows[0].id}
    assert {item.plate_id for item in items} == {15}
    assert staging_litter() == []


# --------------------------------------------------------------------------- #
# §5 step 1 — the long transaction is released before the copy
# --------------------------------------------------------------------------- #


async def test_the_long_transaction_is_released_before_the_copy(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A copy may take minutes, so the request's transaction must be gone first.

    Two halves. The load-bearing one is the caller's session having **no open
    transaction** while the capture runs — on PostgreSQL that is the difference
    between an idle connection and an ``idle in transaction`` one holding its
    snapshot (and, on SQLite, the write lock) for the whole walk of the share.
    The second is a write from another session landing durably mid-copy; under
    the in-memory SQLite the suite runs on it cannot *block* (one connection is
    shared), so it proves only that the producer's own commit does not undo it.
    """
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    observed: dict[str, object] = {}
    real_capture = queue_sources.capture

    async def spy(request):
        observed["in_transaction"] = db_session.in_transaction()
        async with sessions() as probe:
            probe.add(
                LibraryFile(filename="probe.3mf", file_path=str(tmp_path / "probe.3mf"), file_size=1, file_type="gcode")
            )
            await probe.commit()
        return await real_capture(request)

    monkeypatch.setattr(queue_sources, "capture", spy)

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15, quantity=2),
        None,
    )

    assert observed["in_transaction"] is False
    assert len(items) == 2
    probe_row = (await db_session.execute(select(LibraryFile).where(LibraryFile.filename == "probe.3mf"))).scalar_one()
    assert probe_row.id is not None


# --------------------------------------------------------------------------- #
# The print dialog's Quantity burst (drift #3): N concurrent adds, one file
# --------------------------------------------------------------------------- #


async def test_a_burst_of_adds_for_one_file_still_leaves_one_blob(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """One click, several printers: concurrent adds converge on one object.

    The dialog issues one request per (plate, printer) with its own quantity, so
    the honest outcome is pinned in full: two captures of the same bytes publish
    **one** blob with no unique-key 500, and a third add that arrives with both
    worker slots held is refused with §6's ``503 source_copy_busy`` and leaves
    nothing — not a row, not a partial batch, not a stray ``.part``.
    """
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    # A second printer, because the dialog's burst is one request per target —
    # and a printer's queue always carries the printer's own id (the invariant).
    other = await printer_factory(model="P1P", name="second")
    second_queue = PrinterQueue(id=other.id, printer_id=other.id)
    db_session.add(second_queue)
    await db_session.commit()

    gate = threading.Event()
    real_open = queue_sources._open_source

    def blocking_open(path):
        handle = real_open(path)
        gate.wait(10)
        return handle

    monkeypatch.setattr(queue_sources, "_open_source", blocking_open)

    async def add(queue_id: int, quantity: int):
        async with sessions() as session:
            return await add_items_to_printer_queue(
                session,
                PrintQueueItemCreate(queue_id=queue_id, library_file_id=source.id, plate_id=15, quantity=quantity),
                None,
            )

    first = asyncio.create_task(add(queue.id, 2))
    second = asyncio.create_task(add(second_queue.id, 3))
    try:
        deadline = time.monotonic() + 10
        while queue_sources.active_captures() < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert queue_sources.active_captures() == 2

        with pytest.raises(HTTPException) as refused:
            await add(queue.id, 1)
        assert refused.value.status_code == 503
        assert refused.value.detail["code"] == "source_copy_busy"
    finally:
        gate.set()
    first_items, second_items = await first, await second

    assert len(first_items[0]) == 2 and len(second_items[0]) == 3
    assert len(reads) == 2, "two independent adds may each read the share"
    rows = await blobs(db_session)
    assert len(rows) == 1, "…but they leave exactly one final copy"
    assert {item.queue_source_id for item in [*first_items[0], *second_items[0]]} == {rows[0].id}
    assert (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar() == 5
    assert staging_litter() == []


# --------------------------------------------------------------------------- #
# Direct print: copy first, re-check availability, then claim
# --------------------------------------------------------------------------- #


async def test_a_direct_print_copies_before_it_claims_the_printer(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """The claim row is written after the copy, and it carries the snapshot."""
    from backend.app.services import background_dispatch as bd

    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    claimed_while_copying: list[bool] = []
    real_open = queue_sources._open_source

    def watching_open(path):
        # The printer must be free while the bytes are being read: the whole
        # point of copying before the claim is that a stalled share cannot park
        # a machine. Read from a session of its own — the claim, when it comes,
        # is written by the publication's transaction.
        async def look():
            async with sessions() as probe:
                rows = (await probe.execute(select(PrintQueueItem))).scalars().all()
                row = (await probe.execute(select(PrinterQueue).where(PrinterQueue.id == queue.id))).scalar_one()
                return bool(rows) or row.status == "printing"

        claimed_while_copying.append(asyncio.run_coroutine_threadsafe(look(), loop).result(5))
        return real_open(path)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(queue_sources, "_open_source", watching_open)
    monkeypatch.setattr(bd, "async_session", sessions)
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    service = bd.BackgroundDispatchService()

    result = await service._dispatch(
        kind="print_library_file",
        source_id=source.id,
        source_name=source.filename,
        printer_id=printer.id,
        printer_name=printer.name,
        options={"plate_id": 15},
        requested_by_user_id=None,
        requested_by_username=None,
    )

    assert result["status"] == "dispatched"
    assert claimed_while_copying == [False]
    assert len(reads) == 1
    rows = await blobs(db_session)
    assert len(rows) == 1
    claim = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    assert claim.status == "printing" and claim.origin == "direct"
    assert claim.queue_source_id == rows[0].id
    assert claim.source_snapshot["display_filename"] == source.filename
    await db_session.refresh(queue)
    assert queue.status == "printing" and queue.current_item_id == claim.id
    assert staging_litter() == []


async def test_a_printer_that_became_busy_during_the_copy_is_not_claimed(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """Availability is re-checked after the copy; a refusal leaves no queue copy."""
    from backend.app.services import background_dispatch as bd

    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    monkeypatch.setattr(bd, "async_session", sessions)
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    service = bd.BackgroundDispatchService()
    answers = iter([False, True])
    monkeypatch.setattr(service, "_printer_is_busy_printing", lambda printer_id: next(answers, True))

    with pytest.raises(bd.DispatchEnqueueRejected):
        await service._dispatch(
            kind="print_library_file",
            source_id=source.id,
            source_name=source.filename,
            printer_id=printer.id,
            printer_name=printer.name,
            options={"plate_id": 15},
            requested_by_user_id=None,
            requested_by_username=None,
        )

    assert len(reads) == 1, "the copy ran before the claim — that is why the re-check exists"
    assert (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar() == 0
    assert await blobs(db_session) == []
    assert staging_litter() == []
    await db_session.refresh(queue)
    assert queue.status != "printing"
    assert not service._queued_jobs


async def test_a_reprint_whose_archive_is_gone_captures_nothing(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """A reprint resolves an ARCHIVE id, and nothing else — never a library row.

    Archive ids and library ids are independent sequences, so an id that names no
    archive can name a real, unrelated library file. A lookup that fell through to
    one would capture a stranger's bytes and dispatch them under the reprint's
    name. Here the library file carries exactly the id the reprint asks for.
    """
    from backend.app.services import background_dispatch as bd

    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    monkeypatch.setattr(bd, "async_session", sessions)
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    service = bd.BackgroundDispatchService()

    with pytest.raises(HTTPException) as refused:
        await service._dispatch(
            kind="reprint_archive",
            source_id=source.id,
            source_name=source.filename,
            printer_id=printer.id,
            printer_name=printer.name,
            options={"plate_id": 15},
            requested_by_user_id=None,
            requested_by_username=None,
        )

    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "source_unreadable"
    assert reads == [], "nothing may be read for a source that does not exist"
    assert await blobs(db_session) == []
    assert (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar() == 0
    assert staging_litter() == []
    assert not service._queued_jobs


# --------------------------------------------------------------------------- #
# §5 step 4 — the requested plate is checked on the CAPTURED bytes
# --------------------------------------------------------------------------- #


async def test_the_plate_is_resolved_by_the_child_metadata_index_not_a_plate_attribute(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """A file whose only plate is numbered 2 accepts plate 2 and refuses plate 1.

    Bambu names a plate with ``<metadata key="index" value="N"/>`` *inside*
    ``<plate>``; a ``plate_idx`` attribute lookup misses and silently falls back
    to plate 1 (CLAUDE.md's invariant). Then this add would have succeeded — with
    the wrong plate — instead of refusing.
    """
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    plate_two = await a_source(db_session, tmp_path, name="second.gcode.3mf", plates={2: PLATE_FILAMENTS})

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=plate_two.id, plate_id=2),
        None,
    )
    assert [item.plate_id for item in items] == [2]

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(
            db_session,
            PrintQueueItemCreate(queue_id=queue.id, library_file_id=plate_two.id, plate_id=1),
            None,
        )
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "plate_not_found"
    assert len(reads) == 2
    assert (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar() == 1


@pytest.mark.parametrize("tier", ["printer", "auto"])
async def test_a_plate_that_does_not_exist_leaves_no_blob_and_no_job(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads, tier
):
    """The refusal sits between ``capture`` and ``publish``: nothing is written.

    Zero ``queue_sources`` rows and zero job rows, in both tiers — the staged
    bytes are discarded rather than published and then reasoned about.
    """
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()

    with pytest.raises(HTTPException) as refused:
        if tier == "printer":
            await add_items_to_printer_queue(
                db_session,
                PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=99),
                None,
            )
        else:
            await add_items_to_auto_queue(db_session, AutoQueueItemCreate(library_file_id=source.id, plate_id=99), None)

    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "plate_not_found"
    assert len(reads) == 1, "the plate is checked on the copy, so the copy happened"
    assert await blobs(db_session) == []
    assert (await db_session.execute(select(func.count()).select_from(PrintQueueItem))).scalar() == 0
    assert (await db_session.execute(select(func.count()).select_from(AutoQueueItem))).scalar() == 0
    assert staging_litter() == []
    objects = queue_sources.objects_root()
    assert list(objects.rglob("*")) == [] if objects.is_dir() else True


async def test_the_requirements_are_read_from_the_copy_not_the_original(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Step 4 in one assertion: the original is gone before requirements are read.

    Deleting it the moment the bytes have been copied is the only way to tell
    "the evidence came from the staged file" from "it came from the file on the
    share and the staged copy was written beside it".
    """
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    original = Path(source.file_path)
    real_capture = queue_sources.capture

    async def capture_then_lose_the_original(request):
        staged = await real_capture(request)
        original.unlink()
        return staged

    monkeypatch.setattr(queue_sources, "capture", capture_then_lose_the_original)

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15),
        None,
    )

    assert len(items) == 1
    assert not original.exists()
    # The evidence the routing carries had to come from somewhere.
    assert items[0].filament_routing and '"resolved_plate_id":15' in items[0].filament_routing
    assert items[0].queue_source_id == (await blobs(db_session))[0].id


async def test_a_lost_original_costs_nothing_the_job_needs(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The original vanishing inside the capture window must cost the job nothing.

    The routing intent records **no file revision** for a captured source at all
    (see ``queue_source_capture.staged_requirements``: an mtime is the wrong
    identity for a frozen copy, and routing v2 puts the snapshot's hash there),
    so an original that disappears between the copy and the rows takes nothing
    with it. What must survive is the **resolved plate**: it came out of the
    captured bytes, and preflight refuses with ``plate_selection_required`` when
    the stored intent has none.
    """
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    original = Path(source.file_path)
    real_capture = queue_sources.capture

    async def capture_then_lose_the_original(request):
        staged = await real_capture(request)
        original.unlink()
        return staged

    monkeypatch.setattr(queue_sources, "capture", capture_then_lose_the_original)

    items, _batch_id = await enqueue_batch_copies(
        db_session, printer_id=printer.id, count=2, library_file_id=source.id, plate_id=15
    )

    assert len(items) == 2
    stored = json.loads(items[0].filament_routing)
    assert stored["resolved_plate_id"] == 15
    assert stored["source_identity"].get("revision") is None, "a captured source records no revision"
    assert {item.plate_id for item in items} == {15}


def test_the_loop_and_the_workers_survived():
    """A leaked capture thread is a defect, not noise — asserted last, as in Task 2."""
    assert queue_sources.active_captures() == 0
    assert not [t for t in threading.enumerate() if t.name.startswith(queue_sources.THREAD_NAME_PREFIX)]
