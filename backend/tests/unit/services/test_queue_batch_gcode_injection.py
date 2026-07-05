"""Fix C (#1516): reprint / direct-print with quantity > 1 injects EVERY copy.

``enqueue_batch_copies`` is the qty>1 plumbing point shared by the archive
reprint route (``POST /archives/{id}/reprint``) and the library direct-print
route (``POST /library/files/{id}/print``). It must stamp ``gcode_injection``
onto every ``PrintQueueItem`` so the scheduler injects each copy — before this
fix the field was silently dropped and only copies 2…N (or none) were injected.
"""

import pytest

from backend.app.models.library import LibraryFile
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.queue_batch import enqueue_batch_copies


async def _make_queue_and_file(db_session, printer_factory):
    printer = await printer_factory()
    queue = PrinterQueue(printer_id=printer.id)
    lib = LibraryFile(
        filename="x.gcode.3mf",
        file_path="library/files/x.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash="deadbeef",
    )
    db_session.add_all([queue, lib])
    await db_session.commit()
    await db_session.refresh(lib)
    return printer, lib


@pytest.mark.asyncio
@pytest.mark.integration
async def test_batch_copies_all_carry_gcode_injection(db_session, printer_factory):
    printer, lib = await _make_queue_and_file(db_session, printer_factory)
    items, batch_id = await enqueue_batch_copies(
        db_session,
        printer_id=printer.id,
        count=3,
        library_file_id=lib.id,
        gcode_injection=True,
    )
    assert batch_id is not None
    assert len(items) == 3
    # Every copy — including the first — carries the injection flag.
    assert all(it.gcode_injection is True for it in items)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_batch_copies_default_off(db_session, printer_factory):
    printer, lib = await _make_queue_and_file(db_session, printer_factory)
    items, _ = await enqueue_batch_copies(
        db_session,
        printer_id=printer.id,
        count=2,
        library_file_id=lib.id,
    )
    assert len(items) == 2
    assert all(it.gcode_injection is False for it in items)
