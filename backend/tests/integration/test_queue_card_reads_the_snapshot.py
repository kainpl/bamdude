"""The queue card is answered by the snapshot — spec §4 / A09.

A job whose original row is gone already dispatches and prints (Task 6). What it
lost was its *description*: ``queue_times`` and the two response builders read
``archive.file_path`` / ``library_file.file_metadata``, so such a row came back
with no estimate, no weight, no build plate and the name ``File #undefined``.

Everything here goes through the **real API** rather than asserting on a service
return value, because the claim is about what the already-merged card receives:
``print_time_seconds``, ``filament_used_grams``, ``bed_type`` and the name are
the four fields ``QueueCard`` / ``copyQueue`` actually read. The original is both
deleted *and* trapped, so a reader that still tried to open it would say so.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import STATE_READY, QueueSource
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources, queue_times
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_dispatch_without_original import forbid_reads
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration

PLATE = 15
BED = "Textured PEI Plate"
CAPTURED_SECONDS = 3600
CAPTURED_GRAMS = 12.5
RESLICED_SECONDS = 7200


@pytest.fixture(autouse=True)
def clean_spool_state():
    """Process-global capture state must not travel between tests."""
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture(autouse=True)
def clean_plate_cache():
    """Both plate caches are module-level; a hit from another test hides a read."""
    queue_times._PLATE_META_CACHE.clear()
    queue_times._PLATE_PICTURE_CACHE.clear()
    yield
    queue_times._PLATE_META_CACHE.clear()
    queue_times._PLATE_PICTURE_CACHE.clear()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """``queue_sources.publish`` owns its transaction, so it needs this engine."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def a_sliced_file(path: Path, *, prediction: int = CAPTURED_SECONDS, grams=CAPTURED_GRAMS) -> Path:
    return write_routing_3mf(
        path,
        {PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": str(grams)}]},
        model="P1P",
        prediction=prediction,
        bed_type=BED,
    )


async def a_library_source(db, tmp_path, *, name="lamp.gcode.3mf") -> LibraryFile:
    """A library row whose own ``file_metadata`` deliberately says nothing.

    The point of the tests below is which SOURCE answered, so the row carries no
    estimate of its own: a number that came out of the row rather than out of the
    bytes would be indistinguishable from the right answer.
    """
    path = a_sliced_file(tmp_path / name)
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    db.add(row)
    await db.commit()
    return row


async def an_archive_source(db, tmp_path, printer_id: int, *, name="shelf.gcode.3mf") -> PrintArchive:
    folder = settings.archive_dir / "source"
    folder.mkdir(parents=True, exist_ok=True)
    path = a_sliced_file(folder / name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = PrintArchive(
        printer_id=printer_id,
        filename=path.name,
        file_path=str(path.relative_to(settings.base_dir)),
        file_size=path.stat().st_size,
        content_hash=digest,
        source_content_hash=digest,
        status="completed",
        plate_index=PLATE,
    )
    db.add(row)
    await db.commit()
    return row


async def one_blob(db) -> QueueSource:
    return (await db.execute(select(QueueSource).order_by(QueueSource.id))).scalars().one()


async def lose_the_original(db, item, original: Path, *, row) -> None:
    item.archive_id = None
    item.library_file_id = None
    await db.delete(row)
    await db.commit()
    original.unlink()


async def queue_rows(client) -> list[dict]:
    response = await client.get("/api/v1/queue/")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# The per-printer queue
# --------------------------------------------------------------------------- #


async def test_the_card_times_names_and_plates_a_job_whose_library_row_is_gone(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A09: the four fields the card reads come out of the frozen copy."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    original = Path(source.file_path)

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)
    assert item.queue_source_id == blob.id
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    row = next(r for r in await queue_rows(async_client) if r["id"] == item.id)

    assert touched == []
    assert (row["archive_id"], row["library_file_id"]) == (None, None)
    assert row["print_time_seconds"] == CAPTURED_SECONDS
    assert row["filament_used_grams"] == pytest.approx(CAPTURED_GRAMS)
    assert row["bed_type"] == BED
    # ``QueueCard`` renders ``archive_name || library_file_name || 'File #' + id``
    # — with neither id there is no third answer, so one of the two has to carry
    # the human name the job was queued under (§4, A04: never the hash).
    assert row["library_file_name"] == "lamp.gcode.3mf"
    # The hash is in the routing intent, where it is the job's identity (v2) — and
    # nowhere a human reads: never as a name, never as a path (§4, §10).
    assert blob.sha256 not in str({k: v for k, v in row.items() if k != "filament_routing"})
    assert row["filament_routing"]["source_identity"]["revision"]["sha256"] == blob.sha256
    # The row owns its bytes, and the response says so now that it loads the blob.
    assert row["source_storage"] == "ready"
    assert row["source_size_bytes"] == blob.size_bytes


async def test_the_card_describes_the_frozen_copy_after_the_original_is_resliced(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A03: an existing job keeps the bytes it accepted — and its description.

    The original row survives here and its file is re-sliced to twice the print
    time. A card that read the original would show the new number for a job that
    is going to print the old plate.
    """
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    a_sliced_file(Path(source.file_path), prediction=RESLICED_SECONDS, grams=99.0)

    row = next(r for r in await queue_rows(async_client) if r["id"] == item.id)
    assert row["print_time_seconds"] == CAPTURED_SECONDS
    assert row["filament_used_grams"] == pytest.approx(CAPTURED_GRAMS)


async def test_the_card_times_and_names_a_reprint_whose_archive_is_gone(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await an_archive_source(db_session, tmp_path, printer.id)
    original = Path(settings.base_dir) / source.file_path

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, archive_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    row = next(r for r in await queue_rows(async_client) if r["id"] == item.id)

    assert touched == []
    assert row["print_time_seconds"] == CAPTURED_SECONDS
    assert row["bed_type"] == BED
    assert row["archive_name"] == "shelf.gcode.3mf"
    assert row["archive_deleted"] is False


async def test_a_legacy_row_is_described_exactly_as_before(
    async_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """No snapshot, no change: the row's own file still answers for it."""
    from backend.app.models.printer_queue import PrinterQueue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    assert isinstance(queue, PrinterQueue)
    source = await a_library_source(db_session, tmp_path, name="old.gcode.3mf")
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, position=1, created_by_id=None)
    db_session.add(item)
    await db_session.commit()

    row = next(r for r in await queue_rows(async_client) if r["id"] == item.id)
    assert row["source_storage"] == "legacy"
    assert row["print_time_seconds"] == CAPTURED_SECONDS
    assert row["bed_type"] == BED
    assert row["library_file_name"] == "old.gcode.3mf"


async def test_the_queue_poll_parses_each_snapshot_once(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A browser polling the queue must not open a ZIP per row per poll.

    The answer is cached where it always was — ``plate_metadata_cached``, keyed by
    the file's own revision — and for a content-addressed object that key can
    never go stale, so the second poll costs nothing.
    """
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, quantity=3),
        None,
    )
    assert len(items) == 3
    await lose_the_original(db_session, items[0], Path(source.file_path), row=source)
    for extra in items[1:]:
        extra.archive_id, extra.library_file_id = None, None
    await db_session.commit()

    opened: list[Path] = []
    real = zipfile.ZipFile

    def counting(file, *args, **kwargs):
        opened.append(Path(str(file)))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(queue_times.zipfile, "ZipFile", counting)
    monkeypatch.setattr("backend.app.utils.threemf_tools.zipfile.ZipFile", counting)

    first = await queue_rows(async_client)
    second = await queue_rows(async_client)
    assert [r["print_time_seconds"] for r in first if r["id"] in {i.id for i in items}] == [CAPTURED_SECONDS] * 3
    assert [r["print_time_seconds"] for r in second if r["id"] in {i.id for i in items}] == [CAPTURED_SECONDS] * 3
    # Three parsers (time, weight, bed) behind one cache entry, plus the namelist
    # read that answers ``source_thumbnail`` (Task 16) behind its own: four opens
    # for six row-renderings, and nothing on the second poll. The count is the
    # incidental half of this assertion — "and nothing after" is the claim.
    assert len(opened) == 4, opened


# --------------------------------------------------------------------------- #
# The forecast and the order page read the same answer
# --------------------------------------------------------------------------- #


async def test_the_farm_forecast_times_a_job_whose_original_is_gone(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from datetime import datetime, timezone

    from backend.app.services import farm_forecast
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    original = Path(source.file_path)
    await lose_the_original(db_session, items[0], original, row=source)

    touched = forbid_reads(monkeypatch, original)
    snapshot = await farm_forecast.load_snapshot(db_session, datetime.now(timezone.utc).replace(tzinfo=None))
    assert touched == []
    assert [row.seconds for machine in snapshot.printers for row in machine.queued] == [CAPTURED_SECONDS]


async def test_the_order_pages_filament_needs_read_the_snapshot(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The staged (auto) tier: what an order still needs comes out of the copy."""
    from backend.app.models.project import Project
    from backend.app.services import filament_needs
    from backend.app.services.auto_queue_add import add_items_to_auto_queue

    _unused, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    project = Project(name="Order 1", status="active")
    db_session.add(project)
    await db_session.commit()

    await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=source.id, plate_ids=[PLATE], target_model="P1P", project_id=project.id),
        None,
    )
    auto = (await db_session.execute(select(AutoQueueItem))).scalars().one()
    original = Path(source.file_path)
    auto.library_file_id = None
    await db_session.delete(source)
    await db_session.commit()
    original.unlink()

    touched = forbid_reads(monkeypatch, original)
    needs = await filament_needs.queued_needs_of(db_session, [project.id], {})
    assert touched == []
    assert [line.grams for line in needs[project.id][0].filaments] == [pytest.approx(CAPTURED_GRAMS)]
    assert [line.material for line in needs[project.id][0].filaments] == ["PLA"]


async def test_an_auto_queue_row_still_names_itself_after_its_library_file_is_gone(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.auto_queue_add import add_items_to_auto_queue

    _unused, printer, _queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=source.id, plate_ids=[PLATE], target_model="P1P"),
        None,
    )
    auto = (await db_session.execute(select(AutoQueueItem))).scalars().one()
    blob = await one_blob(db_session)
    original = Path(source.file_path)
    auto.library_file_id = None
    await db_session.delete(source)
    await db_session.commit()
    original.unlink()

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.get("/api/v1/auto-queue/")
    assert response.status_code == 200, response.text
    row = next(r for r in response.json() if r["id"] == auto.id)
    assert touched == []
    assert row["library_file_name"] == "lamp.gcode.3mf"
    assert row["source_storage"] == "ready"
    assert blob.state == STATE_READY
    assert blob.sha256 not in str(row)


async def test_a_broken_blob_is_reported_as_broken_and_describes_nothing(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """S7: a bad spool fails its OWN job — it never makes the card read another file."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)
    await lose_the_original(db_session, item, Path(source.file_path), row=source)
    (Path(settings.base_dir) / blob.relative_path).unlink()
    blob.state = "broken"
    await db_session.commit()

    row = next(r for r in await queue_rows(async_client) if r["id"] == item.id)
    assert row["source_storage"] == "broken"
    assert row["print_time_seconds"] is None
    assert row["bed_type"] is None
    # The name is the job's, recorded when the bytes were accepted, so it still
    # tells the operator WHICH job died with the object.
    assert row["library_file_name"] == "lamp.gcode.3mf"
