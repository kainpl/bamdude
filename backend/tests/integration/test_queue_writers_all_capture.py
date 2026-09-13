"""The remaining queue writers capture, and the readers answer from the copy.

Spec: ``60-specs/queue-source-spool-spec.md`` §2 (the writer list, and the two
exemptions), §5 (capture before the rows exist), §7 (one descriptor: intake,
requirements, routing preview and eligibility read the same bytes) and S1/S2/S7.

Task 4 converted the three main producers. What is here is everything else that
creates a runnable job — the Telegram bot's two scenes, the virtual printer's two
modes — plus the half that makes those jobs survive the loss of the original: the
requirements reader and the auto-queue's eligibility answer for a row whose
``library_file_id`` and ``archive_id`` are both NULL.

Three shapes recur, each pinning one rule:

* **the read of the ORIGINAL is counted** (``reads``), because content dedup
  means one blob cannot tell one capture from three;
* **a stale id of the wrong kind refuses**, with no read at all — archive ids
  and library ids are independent sequences, so a fall-through would capture a
  stranger's bytes (Task 4's Major, repeated here for the writers that resolve
  their own source);
* **a refusal leaves nothing**: no blob, no job row, no ``.part``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import FORMAT_3MF, QueueSource
from backend.app.services import queue_sources
from backend.app.services.auto_queue_eligibility import find_eligible_printer
from backend.app.services.filament_intake import item_descriptor, read_item_requirements, routing_detail
from backend.app.services.filament_policy_write import routing_update
from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION, source_snapshot
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


PLATE_FILAMENTS = [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "0.0001"}]


# --------------------------------------------------------------------------- #
# Harness (the same seams Task 4's suite uses — see test_queue_add_captures_once)
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
    """The publication owns its own transaction, so it needs the TEST factory."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


@pytest.fixture
def reads(monkeypatch):
    """Every read of an ORIGINAL file, in order — one per capture."""
    seen: list[Path] = []
    real = queue_sources._open_source

    def spy(path: Path):
        seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(queue_sources, "_open_source", spy)
    return seen


async def blobs(db) -> list[QueueSource]:
    return list((await db.execute(select(QueueSource).order_by(QueueSource.id))).scalars())


def object_of(source: QueueSource) -> Path:
    return Path(settings.base_dir) / source.relative_path


def staging_litter() -> list[Path]:
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.is_dir() else []


async def count_of(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar()


def a_callback():
    return SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace())


def a_state(data: dict):
    return SimpleNamespace(get_data=AsyncMock(return_value=data), clear=AsyncMock(), update_data=AsyncMock())


def answered(callback) -> str:
    return callback.answer.await_args.args[0]


async def published_blob(sessions, path: Path) -> QueueSource:
    """A real capture and publication of ``path`` with no job attached to it."""
    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(path=path, format=FORMAT_3MF, display_filename=path.name)
    )

    async def attach(session: AsyncSession, source: QueueSource) -> None:
        return None

    return await queue_sources.publish(receipt, attach, session_factory=sessions)


def snapshot_of(blob: QueueSource, *, filename: str, plate_fallback: int | None = None) -> dict:
    from backend.app.services.queue_source_descriptor import QueueSourceDescriptor

    return source_snapshot(
        QueueSourceDescriptor(
            path=object_of(blob),
            format=blob.format,
            sha256=blob.sha256,
            size_bytes=blob.size_bytes,
            display_filename=filename,
            plate_fallback=plate_fallback,
            provenance={"kind": "library_file", "id": 0},
        )
    )


# --------------------------------------------------------------------------- #
# The Telegram bot — library scene, "add to queue"
# --------------------------------------------------------------------------- #


async def test_the_bots_library_add_queues_from_a_captured_copy(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """``lib:add_queue`` used to build the row with nothing but an id.

    It is the bot's plainest door — no routing, no plate — which is exactly why
    it must still capture: the operator pressed the button, and the job has to
    outlive the laptop the file came from.
    """
    from backend.app.services.telegram_handlers.library_scene import cb_library_add_queue

    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    original = Path(source.file_path)
    callback = a_callback()

    with patch("backend.app.services.telegram_handlers.start.cmd_start", new=AsyncMock()):
        await cb_library_add_queue(callback, a_state({"file_id": source.id, "printer_id": printer.id}), tg_chat=None)

    item = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    rows = await blobs(db_session)
    assert len(rows) == 1
    assert reads == [original]
    assert item.queue_source_id == rows[0].id
    assert item.queue_id == queue.id
    assert item.library_file_id == source.id
    assert item.source_snapshot == {
        "version": SOURCE_SNAPSHOT_VERSION,
        "provenance": {"kind": "library_file", "id": source.id},
        "display_filename": source.filename,
        "format": "3mf",
        "plate_fallback": None,
    }
    assert rows[0].sha256 == hashlib.sha256(original.read_bytes()).hexdigest()
    assert object_of(rows[0]).is_file()
    assert staging_litter() == []
    assert callback.answer.await_args.kwargs.get("show_alert") is not True


async def test_the_bots_library_add_refuses_a_file_id_that_names_an_archive(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """A stale ``file_id`` is a LIBRARY id, and nothing else (obligation A).

    The scene's id came from a library listing; if that row is gone, the same
    number very often names a real, unrelated **archive**, because the two tables
    count independently. Capturing that archive's bytes would queue a stranger's
    model under this file's name, and nothing afterwards would look wrong.
    """
    from backend.app.services.telegram_handlers.library_scene import cb_library_add_queue

    _source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    other = write_routing_3mf(tmp_path / "stranger.gcode.3mf", {1: PLATE_FILAMENTS})
    # An explicit id, because the two sequences are independent: what the scene
    # remembers is a library id, and here that number names only an archive.
    archive = PrintArchive(
        id=4242,
        filename=other.name,
        file_path=str(other),
        file_size=other.stat().st_size,
        print_name="stranger",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    assert (
        await db_session.execute(select(LibraryFile).where(LibraryFile.id == archive.id))
    ).scalar_one_or_none() is None, "fixture no longer reproduces the id collision"
    callback = a_callback()

    with patch("backend.app.services.telegram_handlers.start.cmd_start", new=AsyncMock()):
        await cb_library_add_queue(callback, a_state({"file_id": archive.id, "printer_id": printer.id}), tg_chat=None)

    assert reads == [], "nothing may be read for a library id that names no library file"
    assert await blobs(db_session) == []
    assert await count_of(db_session, PrintQueueItem) == 0
    assert staging_litter() == []
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_the_bot_tells_the_operator_which_refusal_it_was(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Obligation D: "failed" taught an operator nothing about busy or no space.

    The taxonomy is not re-tabulated here — the message is the one the HTTP
    routes answer with, taken off the refusal the capture service raised.
    """
    from backend.app.services.telegram_handlers.library_scene import cb_library_add_queue

    source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()

    async def busy(_request):
        raise queue_sources.QueueSourceBusy()

    monkeypatch.setattr(queue_sources, "capture", busy)
    callback = a_callback()

    with patch("backend.app.services.telegram_handlers.start.cmd_start", new=AsyncMock()):
        await cb_library_add_queue(callback, a_state({"file_id": source.id, "printer_id": printer.id}), tg_chat=None)

    assert answered(callback) == routing_detail("source_copy_busy")["message"]
    assert await count_of(db_session, PrintQueueItem) == 0


# --------------------------------------------------------------------------- #
# The Telegram bot — queue scene, per-printer confirm and the auto-queue target
# --------------------------------------------------------------------------- #


async def test_the_bots_queue_confirm_captures_once_and_routes_off_the_copy(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """``qadd:confirm`` reads routing too — from the captured bytes (§5 step 4)."""
    from backend.app.services.telegram_handlers.queue_scene import cb_qadd_confirm

    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    original = Path(source.file_path)
    real_capture = queue_sources.capture

    async def capture_then_lose_the_original(request):
        staged = await real_capture(request)
        original.unlink()
        return staged

    monkeypatch.setattr(queue_sources, "capture", capture_then_lose_the_original)
    callback = a_callback()

    with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
        await cb_qadd_confirm(callback, a_state({"file_id": source.id, "printer_id": printer.id}), tg_chat=None)

    item = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    rows = await blobs(db_session)
    assert len(rows) == 1 and reads == [original]
    assert not original.exists(), "the evidence below had to come from the copy"
    assert item.queue_source_id == rows[0].id
    assert item.queue_id == queue.id
    assert item.plate_id == 15
    assert item.filament_routing and json.loads(item.filament_routing)["resolved_plate_id"] == 15
    assert item.source_snapshot["display_filename"] == source.filename
    assert staging_litter() == []


async def test_the_bots_auto_queue_target_reports_the_real_refusal(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The auto-queue half of the scene goes through the converted writer already,
    so what is left to prove is that its refusal survives the handler."""
    from backend.app.services.telegram_handlers.queue_scene import _add_to_auto_queue

    source, _printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()

    async def no_space(_request):
        raise queue_sources.NoSpace()

    monkeypatch.setattr(queue_sources, "capture", no_space)
    callback = a_callback()

    with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
        await _add_to_auto_queue(callback, "en", source.id, "P1P", None)

    assert answered(callback) == routing_detail("source_spool_no_space")["message"]
    assert await count_of(db_session, AutoQueueItem) == 0
    assert staging_litter() == []


# --------------------------------------------------------------------------- #
# The virtual printer — both modes
# --------------------------------------------------------------------------- #


async def a_vp(tmp_path, test_engine, printer, *, mode: str) -> VirtualPrinterInstance:
    return VirtualPrinterInstance(
        vp_id=1,
        name="Synthetic",
        mode=mode,
        model="C11",
        access_code="00000000",
        serial_suffix="000000001",
        target_printer_id=printer.id,
        auto_dispatch=True,
        base_dir=tmp_path,
        session_factory=async_sessionmaker(test_engine, expire_on_commit=False),
    )


async def test_a_slicer_send_all_captures_once_for_every_plate(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """A multi-plate Send All is one upload: one capture, one blob, N rows (A01)."""
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    write_routing_3mf(Path(source.file_path), {1: PLATE_FILAMENTS, 2: PLATE_FILAMENTS})
    source.file_metadata = {"sliced_for_model": "P1P", "plates": [{"index": 1}, {"index": 2}]}
    source.file_size = Path(source.file_path).stat().st_size
    await db_session.commit()
    vp = await a_vp(tmp_path, test_engine, printer, mode="print_queue")
    monkeypatch.setattr(vp, "_save_to_library", AsyncMock(return_value=source))
    monkeypatch.setattr(vp, "_find_best_queue", AsyncMock(return_value=queue))

    await vp._add_to_print_queue(Path(source.file_path), "127.0.0.1")

    items = list((await db_session.execute(select(PrintQueueItem).order_by(PrintQueueItem.position))).scalars())
    rows = await blobs(db_session)
    assert len(items) == 2 and len(rows) == 1
    assert reads == [Path(source.file_path)], "one upload is one capture, never one per plate"
    assert {item.queue_source_id for item in items} == {rows[0].id}
    assert [item.plate_id for item in items] == [1, 2]
    assert [item.position for item in items] == [1, 2]
    assert all(item.source_snapshot["display_filename"] == source.filename for item in items)
    assert all(item.queue_id == queue.id for item in items)
    # The late-MQTT registry still names the rows it may retro-stamp.
    assert vp._recent_queue_items[Path(source.file_path).name][0] == [item.id for item in items]
    assert staging_litter() == []


async def test_a_slicer_upload_naming_a_plate_the_file_lacks_leaves_nothing(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """Obligation B, on the VP: the plate is checked between capture and publish."""
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source.file_metadata = {"sliced_for_model": "P1P", "plates": [{"index": 9}]}
    await db_session.commit()
    vp = await a_vp(tmp_path, test_engine, printer, mode="print_queue")
    monkeypatch.setattr(vp, "_save_to_library", AsyncMock(return_value=source))
    monkeypatch.setattr(vp, "_find_best_queue", AsyncMock(return_value=queue))

    await vp._add_to_print_queue(Path(source.file_path), "127.0.0.1")

    assert len(reads) == 1, "the file is copied first — that is what makes the plate check real"
    assert await blobs(db_session) == [], "a refused add publishes nothing"
    assert await count_of(db_session, PrintQueueItem) == 0
    assert staging_litter() == [], "the staged copy must be discarded, not left for the grace window"


async def test_a_farm_distributed_slicer_upload_captures_too(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, reads
):
    """``auto_queue`` mode has no printer yet, and captures exactly the same."""
    source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source.file_metadata = {"sliced_for_model": "P1P", "plates": [{"index": 15}]}
    await db_session.commit()
    original = Path(source.file_path)
    vp = await a_vp(tmp_path, test_engine, printer, mode="auto_queue")
    monkeypatch.setattr(vp, "_save_to_library", AsyncMock(return_value=source))
    real_capture = queue_sources.capture

    async def capture_then_lose_the_original(request):
        staged = await real_capture(request)
        original.unlink()
        return staged

    monkeypatch.setattr(queue_sources, "capture", capture_then_lose_the_original)

    await vp._add_to_auto_queue(original, "127.0.0.1")

    item = (await db_session.execute(select(AutoQueueItem))).scalar_one()
    rows = await blobs(db_session)
    assert len(rows) == 1 and reads == [original]
    assert not original.exists()
    assert item.queue_source_id == rows[0].id
    assert item.plate_id == 15, "resolved out of the captured bytes"
    assert item.target_model == "P1P"
    assert json.loads(item.required_filament_types) == ["PLA"]
    assert item.source_snapshot["display_filename"] == source.filename
    assert vp._recent_auto_items[original.name][0] == [item.id]
    assert staging_litter() == []


# --------------------------------------------------------------------------- #
# §7 — the readers answer from the snapshot, with both original refs NULL
# --------------------------------------------------------------------------- #


async def a_snapshot_job(db, sessions, tmp_path, *, queue_id: int, model=PrintQueueItem, plate_id: int | None = 15):
    """A job that is nothing but its snapshot: both original refs are NULL.

    This is the row every other part of the feature exists for — the library file
    was trashed, the archive purged, the share unplugged — and it must still be
    read, routed and placed.
    """
    original = write_routing_3mf(tmp_path / "orphan.gcode.3mf", {plate_id or 15: PLATE_FILAMENTS})
    blob = await published_blob(sessions, original)
    original.unlink()
    payload = snapshot_of(blob, filename="orphan.gcode.3mf")
    row = (
        PrintQueueItem(
            queue_id=queue_id,
            status="pending",
            position=1,
            plate_id=plate_id,
            queue_source_id=blob.id,
            source_snapshot=payload,
        )
        if model is PrintQueueItem
        else AutoQueueItem(
            status="pending",
            position=1,
            plate_id=plate_id,
            target_model="P1P",
            required_filament_types=json.dumps(["PLA"]),
            queue_source_id=blob.id,
            source_snapshot=payload,
        )
    )
    db.add(row)
    await db.commit()
    return row, blob


async def test_the_requirements_of_a_job_whose_original_rows_are_gone(db_session, tmp_path, printer_factory, sessions):
    """``read_item_requirements`` is the choke point every reader goes through."""
    printer = await printer_factory(model="P1P")
    from backend.app.models.printer_queue import PrinterQueue

    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()
    item, blob = await a_snapshot_job(db_session, sessions, tmp_path, queue_id=printer.id)

    descriptor = await item_descriptor(db_session, item)
    assert descriptor is not None
    assert descriptor.path == object_of(blob) and descriptor.format == "3mf"
    assert descriptor.display_filename == "orphan.gcode.3mf"

    req = await read_item_requirements(db_session, item)
    assert (item.archive_id, item.library_file_id) == (None, None)
    assert req.status == "ok"
    assert req.resolved_plate_id == 15
    assert [f["type"] for f in req.used_filaments] == ["PLA"]


async def test_the_snapshots_plate_fallback_answers_for_the_archive_that_is_gone(
    db_session, tmp_path, printer_factory, sessions
):
    """An archive-sourced job that names no plate used to read ``archive.plate_index``.

    That row can be purged, so the fallback travels in the snapshot — and it has to
    be read from there, or a multi-plate reprint would go back to being ambiguous
    (or, worse, silently resolve to plate 1: the mistake that made ``plate_number``
    inert for months).
    """
    from backend.app.models.printer_queue import PrinterQueue

    printer = await printer_factory(model="P1P")
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    original = write_routing_3mf(tmp_path / "two-plates.gcode.3mf", {1: PLATE_FILAMENTS, 2: PLATE_FILAMENTS})
    blob = await published_blob(sessions, original)
    original.unlink()
    item = PrintQueueItem(
        queue_id=printer.id,
        status="pending",
        position=1,
        plate_id=None,
        queue_source_id=blob.id,
        source_snapshot=snapshot_of(blob, filename="two-plates.gcode.3mf", plate_fallback=2),
    )
    db_session.add(item)
    await db_session.commit()

    req = await read_item_requirements(db_session, item)

    assert req.status == "ok", req.reason
    assert req.resolved_plate_id == 2


async def test_a_legacy_job_with_no_source_at_all_still_refuses(db_session, tmp_path, printer_factory, sessions):
    """The other half of the same assertion: it is the SNAPSHOT that made it work.

    A row with neither a snapshot nor an original is the pre-m173 shape after a
    hard delete, and it must keep reporting a refusal rather than acquiring a
    source from somewhere.
    """
    printer = await printer_factory(model="P1P")
    from backend.app.models.printer_queue import PrinterQueue

    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    item = PrintQueueItem(queue_id=printer.id, status="pending", position=1)
    db_session.add(item)
    await db_session.commit()

    assert await item_descriptor(db_session, item) is None
    req = await read_item_requirements(db_session, item)
    assert req.status != "ok" and req.reason == "source_unreadable"


async def test_eligibility_places_a_job_whose_library_row_is_gone(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A17/A02 for the distributor: routing must not need the original row."""
    _source, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    item, _blob = await a_snapshot_job(db_session, sessions, tmp_path, queue_id=printer.id, model=AutoQueueItem)

    result = await find_eligible_printer(db_session, item, set())

    assert result.printer is not None, result.reason
    assert result.printer.id == printer.id
    assert item.plate_id == 15


async def test_a_captured_job_stores_no_file_revision_and_dispatch_never_asks_for_one(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A03/S2 at the preflight boundary, and why the stored revision had to go.

    The routing intent used to carry the ORIGINAL's ``(size, mtime_ns)`` so that
    dispatch could refuse a job whose source had changed underneath it. For a job
    that prints a frozen copy that question is answered differently — the old job
    keeps the bytes it accepted — and asking the old way is worse than useless:
    the copy's mtime is not the original's, so every captured job would defer as
    ``source_changed``, and after a restore an mtime means nothing at all.

    So: no revision is recorded, preflight skips the comparison exactly as it does
    for rows written before revisions existed, and the job preflights fine with
    the original **deleted**.
    """
    from backend.app.schemas.print_queue import PrintQueueItemCreate
    from backend.app.services.filament_preflight import preflight_item
    from backend.app.services.queue_add import add_items_to_printer_queue

    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15),
        None,
    )
    item = items[0]
    stored = json.loads(item.filament_routing)
    assert stored["source_identity"].get("revision") is None
    assert stored["resolved_plate_id"] == 15, "the plate is not the revision's to take with it"

    Path(source.file_path).unlink()
    guard = await preflight_item(db_session, item, printer.id)

    assert guard is not None and guard.plan is not None
    assert guard.plan.resolved_plate_id == 15


async def test_an_edit_of_a_snapshot_job_re_reads_the_snapshot(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """``routing_update`` re-reads the source on any scope change.

    Its source used to be the original, so editing a queued job — a different
    plate, another printer, AMS off — stopped working the moment the library row
    was trashed or the share went away, which is the opposite of what a
    self-contained job promises. Here the row is written by the real add and then
    loses BOTH its library row and the file on disk.
    """
    from backend.app.schemas.print_queue import PrintQueueItemCreate
    from backend.app.services.queue_add import add_items_to_printer_queue

    source, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    await db_session.commit()
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=15),
        None,
    )
    item = items[0]
    assert item.queue_source_id is not None
    item.library_file_id = None
    await db_session.delete(source)
    await db_session.commit()
    Path(source.file_path).unlink()

    changes = await routing_update(db_session, item, {"plate_id": 15, "use_ams": False})

    assert changes["plate_id"] == 15
    assert changes["filament_routing"]
    assert json.loads(changes["filament_routing"])["resolved_plate_id"] == 15
    assert json.loads(changes["filament_routing"])["feed_policy"] == "external_only"
