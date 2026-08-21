"""The queue row learns the plate at the same moment the archive does.

⚠️ Reported from a farm: a print started from BambuStudio was picked up, its
3MF downloaded, its row added — and the row's ``plate_id`` was NULL.

Not a plumbing fault. At the moment the row is created the plate is genuinely
unknowable: the printer reports the file as ``Cube_slicer.gcode.3mf`` with no
plate in the name and no ``gcode_file`` beside it, so ``live_plate_id`` is None
and stays None. The plate only exists once the 3MF has been fetched and parsed —
which is exactly where ``attach_3mf_to_archive`` already backfills
``archive.plate_index``.

That row is repeatable, and a repeat with no plate prints plate 1. So the row is
carried along with the archive, from the one place both callers of the attach
pass through — ``on_print_start``'s own download and the retry service.
"""

from pathlib import Path

import pytest

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation
# relationship by string and SQLAlchemy cannot resolve it unless this module has
# been imported.
import backend.app.models.printer_location  # noqa: F401
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.archive import ArchiveService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _print_being_picked_up(db_session, printer_factory, tmp_path: Path, *, plate, row_plate):
    """An external print mid-pickup: archive row exists, 3MF has not landed."""
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)

    archive = PrintArchive(
        printer_id=printer.id,
        filename="Cube_slicer.gcode.3mf",
        file_path="",
        file_size=0,
        content_hash=None,
        status="printing",
        plate_index=plate,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    row = PrintQueueItem(
        queue_id=queue.id,
        status="printing",
        position=0,
        archive_id=archive.id,
        plate_id=row_plate,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    src = tmp_path / "Cube_slicer.gcode.3mf"
    src.write_bytes(b"picked-up-bytes")
    return archive, row, src


async def test_the_row_gets_the_plate_when_the_file_lands(db_session, printer_factory, tmp_path):
    archive, row, src = await _print_being_picked_up(db_session, printer_factory, tmp_path, plate=2, row_plate=None)

    ok = await ArchiveService(db_session).attach_3mf_to_archive(
        archive.id, src, original_filename="Cube_slicer.gcode.3mf"
    )

    assert ok is True
    await db_session.refresh(row)
    assert row.plate_id == 2


async def test_a_plate_already_on_the_row_is_never_overwritten(db_session, printer_factory, tmp_path):
    """⚠️ Mirrors the archive's own rule one line above: backfill only.

    A value on the row was chosen — by the dispatcher, or by whoever queued it —
    and the container holds several plates, so what we parse out of it cannot
    overrule that.
    """
    archive, row, src = await _print_being_picked_up(db_session, printer_factory, tmp_path, plate=2, row_plate=1)

    await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, original_filename="Cube_slicer.gcode.3mf")

    await db_session.refresh(row)
    assert row.plate_id == 1


async def test_an_unknown_plate_writes_nothing(db_session, printer_factory, tmp_path):
    """The 3MF could not say either — the row stays NULL rather than gaining a 1."""
    archive, row, src = await _print_being_picked_up(db_session, printer_factory, tmp_path, plate=None, row_plate=None)

    await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, original_filename="Cube_slicer.gcode.3mf")

    await db_session.refresh(row)
    assert row.plate_id is None


async def test_another_prints_row_is_left_alone(db_session, printer_factory, tmp_path):
    """⚠️ The link is ``archive_id``, not the printer.

    Matching on the printer would reach every row it ever ran, including the one
    waiting for a plate answer from the print before this one.
    """
    archive, row, src = await _print_being_picked_up(db_session, printer_factory, tmp_path, plate=2, row_plate=None)
    stranger = PrintQueueItem(queue_id=row.queue_id, status="completed", position=0, archive_id=None, plate_id=None)
    db_session.add(stranger)
    await db_session.commit()
    await db_session.refresh(stranger)

    await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, original_filename="Cube_slicer.gcode.3mf")

    await db_session.refresh(stranger)
    assert stranger.plate_id is None
