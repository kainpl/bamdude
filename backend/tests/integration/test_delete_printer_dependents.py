"""Deleting a printer takes everything that cannot outlive it.

Reported from a user's log bundle (2026-08-17): ``DELETE /printers/6`` answered
500 while ``DELETE /printers/3`` and ``/printers/7`` answered 200 on the same
install minutes apart. The difference was that printer 6 still had rows in
``print_queue``:

    sqlite3.IntegrityError: NOT NULL constraint failed: print_queue.queue_id
      ... printers.py line 594, in delete_printer -> await db.commit()

``Printer.queue`` cascades to the ``printer_queues`` row, but
``PrinterQueue.items`` had no cascade — so the ORM tried to *de-associate* the
queue's items by nulling ``queue_id``, which is NOT NULL. The printer therefore
became undeletable for as long as it had any queue history at all: not just
pending work, any row, including long-completed ones.

⚠️ The second half of this is quieter and was found while fixing the first.
**SQLite does not enforce foreign keys** (no ``PRAGMA foreign_keys=ON``
anywhere), so every ``ondelete="CASCADE"`` pointing at ``printers.id`` is
decorative there. Twelve tables carry a NOT NULL ``printer_id`` with no ORM
relationship to clean them up, so each deleted printer left rows behind that
point at a printer that no longer exists — silently, on the default database.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.printer_setting_audit import PrinterSettingAudit


async def _queue_with_item(db, printer_id: int, *, status: str = "pending") -> PrinterQueue:
    queue = PrinterQueue(printer_id=printer_id, status="idle")
    db.add(queue)
    await db.flush()
    db.add(PrintQueueItem(queue_id=queue.id, position=1, status=status))
    await db.flush()
    return queue


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_queue_history_can_be_deleted(async_client: AsyncClient, printer_factory, db_session):
    """The reported 500, as a test."""
    printer = await printer_factory(name="Doomed")
    await _queue_with_item(db_session, printer.id, status="completed")
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/printers/{printer.id}?delete_archives=true")

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_pending_work_can_be_deleted(async_client: AsyncClient, printer_factory, db_session):
    printer = await printer_factory(name="Doomed too")
    await _queue_with_item(db_session, printer.id, status="pending")
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/printers/{printer.id}?delete_archives=true")

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_queue_and_its_items_are_gone(async_client: AsyncClient, printer_factory, db_session):
    printer = await printer_factory(name="Doomed three")
    # ⚠️ Read the ids BEFORE the delete. ``expire_all`` below forces a refresh on
    # next access, and a deleted row cannot be refreshed — the attribute access
    # raises MissingGreenlet rather than returning the number we wanted.
    printer_id = printer.id
    queue = await _queue_with_item(db_session, printer_id)
    queue_id = queue.id
    await db_session.commit()

    await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=true")

    db_session.expire_all()
    items = await db_session.execute(
        select(func.count()).select_from(PrintQueueItem).where(PrintQueueItem.queue_id == queue_id)
    )
    queues = await db_session.execute(
        select(func.count()).select_from(PrinterQueue).where(PrinterQueue.printer_id == printer_id)
    )
    assert items.scalar() == 0, "queue items must not outlive the queue they belong to"
    assert queues.scalar() == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_rows_whose_fk_says_cascade_are_actually_gone(async_client: AsyncClient, printer_factory, db_session):
    """SQLite ignores ``ondelete``, so the route has to honour it by hand."""
    printer = await printer_factory(name="Doomed four")
    printer_id = printer.id
    db_session.add(
        FilamentCalibration(
            printer_id=printer_id,
            filament_id="GFA00",
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            cali_mode="manual",
            source="manual",
            name="test",
        )
    )
    db_session.add(
        PrinterSettingAudit(printer_id=printer_id, tab="print", action="set", payload_json="{}", result="ok")
    )
    await db_session.commit()

    await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=true")

    db_session.expire_all()
    for model in (FilamentCalibration, PrinterSettingAudit):
        left = await db_session.execute(select(func.count()).select_from(model).where(model.printer_id == printer_id))
        assert left.scalar() == 0, f"{model.__tablename__} still points at a printer that is gone"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_archive_files_go_with_the_rows(async_client: AsyncClient, printer_factory, db_session, tmp_path):
    """``delete_archives=true`` deleted the rows with one bulk statement and left
    every file on disk, permanently — the archive folder grew by a whole printer
    each time somebody removed one, findable only by the orphan-prune script."""
    from backend.app.core.config import settings
    from backend.app.models.archive import PrintArchive

    printer = await printer_factory(name="Doomed five")
    printer_id = printer.id
    archive_dir = settings.archive_dir / "20260818_120000_doomed"
    archive_dir.mkdir(parents=True, exist_ok=True)
    threemf = archive_dir / "job.gcode.3mf"
    threemf.write_bytes(b"not really a zip, but it is on disk")
    # ⚠️ The timelapse, the thumbnail and the rest live INSIDE the archive
    # folder — ``attach_timelapse`` writes through ``safe_join_under(archive_dir,
    # ...)``. So they are covered by the same removal, and this asserts it
    # rather than trusting the docstring that says so.
    timelapse = archive_dir / "timelapse.mp4"
    timelapse.write_bytes(b"not really an mp4")
    thumbnail = archive_dir / "thumbnail.png"
    thumbnail.write_bytes(b"not really a png")
    db_session.add(
        PrintArchive(
            printer_id=printer_id,
            file_path=str(threemf.relative_to(settings.base_dir)),
            file_size=threemf.stat().st_size,
            print_name="job",
            filename="job.gcode.3mf",
            status="completed",
            timelapse_path=str(timelapse.relative_to(settings.base_dir)),
            thumbnail_path=str(thumbnail.relative_to(settings.base_dir)),
        )
    )
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=true")

    assert response.status_code == 200, response.text
    assert not threemf.exists(), "the 3MF outlived the archive row that pointed at it"
    assert not timelapse.exists(), "the timelapse outlived its archive"
    assert not thumbnail.exists(), "the thumbnail outlived its archive"
    assert not archive_dir.exists(), "the archive folder was left behind"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_archive_already_in_the_trash_goes_too(async_client: AsyncClient, printer_factory, db_session):
    """Its files as well as its row.

    ⚠️ There is deliberately no bulk statement behind the per-archive loop, so
    this is the only thing asserting the rows actually go. A fallback sweep is
    what let the archive trash look healthy for four months while it removed
    nothing from disk.
    """
    from datetime import datetime, timezone

    from backend.app.core.config import settings
    from backend.app.models.archive import PrintArchive

    printer = await printer_factory(name="Doomed six")
    printer_id = printer.id
    archive_dir = settings.archive_dir / "20260818_150000_trashed"
    archive_dir.mkdir(parents=True, exist_ok=True)
    threemf = archive_dir / "job.gcode.3mf"
    threemf.write_bytes(b"trashed but still on disk")
    db_session.add(
        PrintArchive(
            printer_id=printer_id,
            file_path=str(threemf.relative_to(settings.base_dir)),
            file_size=threemf.stat().st_size,
            print_name="job",
            filename="job.gcode.3mf",
            status="completed",
            deleted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=true")

    assert response.status_code == 200, response.text
    assert not threemf.exists()
    assert not archive_dir.exists()
    db_session.expire_all()
    left = await db_session.execute(
        select(func.count()).select_from(PrintArchive).where(PrintArchive.printer_id == printer_id)
    )
    assert left.scalar() == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_file_another_printer_still_points_at_survives(async_client: AsyncClient, printer_factory, db_session):
    """Disk storage is deduplicated by content hash, so one folder can back
    archives on several printers. Deleting one of those printers must not take
    the file out from under the others.

    ``ArchiveService.delete_archive`` counts other rows sharing the same
    ``file_path`` — across every printer, not just this one — and only removes
    the folder when the last of them goes. This is the test that says so from
    the printer-delete side, because that path deletes archives in a loop and
    the count has to hold at every step of it.
    """
    from backend.app.core.config import settings
    from backend.app.models.archive import PrintArchive

    doomed = await printer_factory(name="Goes away")
    keeper = await printer_factory(name="Stays")
    doomed_id, keeper_id = doomed.id, keeper.id

    shared_dir = settings.archive_dir / "20260818_130000_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    shared_file = shared_dir / "same-bytes.gcode.3mf"
    shared_file.write_bytes(b"one file, two archives")
    relative = str(shared_file.relative_to(settings.base_dir))

    for printer_id in (doomed_id, keeper_id):
        db_session.add(
            PrintArchive(
                printer_id=printer_id,
                file_path=relative,
                file_size=shared_file.stat().st_size,
                print_name="shared",
                filename="same-bytes.gcode.3mf",
                status="completed",
                content_hash="deadbeef",
            )
        )
    await db_session.commit()

    await async_client.delete(f"/api/v1/printers/{doomed_id}?delete_archives=true")

    assert shared_file.exists(), "the other printer's archive still points at this file"
    db_session.expire_all()
    survivors = await db_session.execute(select(PrintArchive).where(PrintArchive.file_path == relative))
    rows = survivors.scalars().all()
    assert [row.printer_id for row in rows] == [keeper_id]

    # ...and the last one out takes the folder with it.
    await async_client.delete(f"/api/v1/printers/{keeper_id}?delete_archives=true")

    assert not shared_file.exists()
    assert not shared_dir.exists()


@pytest.mark.integration
def test_every_fk_to_printers_is_accounted_for():
    """The drift guard.

    A new table with a NOT NULL ``printer_id`` and no cleanup is invisible until
    somebody deletes a printer and either gets a 500 or quietly orphans rows.
    Adding one now fails here instead, with the name of the table.
    """
    import backend.app.models  # noqa: F401  - populate the registry
    import backend.app.models.auto_queue  # noqa: F401
    from backend.app.api.routes.printers import PRINTER_CASCADE_MODELS
    from backend.app.core.database import Base

    handled = {model.__tablename__ for model in PRINTER_CASCADE_MODELS}
    # Cleaned up by an ORM relationship on ``Printer`` instead of by the route.
    by_orm_cascade = {
        "print_archives",
        "printer_maintenance",
        "ams_sensor_history",
        "printer_sensor_history",
        "printer_queues",
    }

    unhandled = set()
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            try:
                if fk.column.table.name != "printers":
                    continue
            except Exception:  # noqa: BLE001 - unresolvable FK is not ours to judge
                continue
            # A nullable column can simply be nulled and keep its row.
            if fk.parent.nullable:
                continue
            if table.name in handled or table.name in by_orm_cascade:
                continue
            unhandled.add(table.name)

    assert not unhandled, (
        f"these tables carry a NOT NULL printer_id with nothing to clean them up: {sorted(unhandled)}. "
        "Add the model to PRINTER_CASCADE_MODELS in routes/printers.py, or give Printer a cascading "
        "relationship and list it here."
    )
