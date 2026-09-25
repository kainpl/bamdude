"""The batch virtual queue read runs on a disposable native PostgreSQL too."""

from types import SimpleNamespace

import pytest
from sqlalchemy import event

from backend.app.api.routes.auto_queue import auto_queue_pending_summary
from backend.app.api.routes.inventory import spool_picker
from backend.app.api.routes.print_queue import queue_issues, queue_summary
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.spool import Spool
from backend.app.services import queue_virtual
from backend.app.services.print_run_binding import bind_print_run, discard_print_run
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_hot_queue_reads import (
    test_virtual_auxiliary_selects_are_batch_bounded as assert_batch_sql,
)
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("embedded_postgres")


@pytest.mark.asyncio
async def test_compact_queue_reads_on_postgres(db_session, printer_factory):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    db_session.add_all(
        [
            PrintQueueItem(queue_id=printer.id, position=1, status="pending"),
            PrintQueueItem(queue_id=printer.id, position=2, status="failed"),
            AutoQueueItem(position=1, status="pending"),
        ]
    )
    await db_session.commit()

    summary = await queue_summary(db_session, (None, True))
    assert summary.pending_count == 1
    assert summary.groups[0].failed_count == 1
    issues = await queue_issues(printer.id, None, 50, db_session, (None, True))
    assert len(issues.items) == 1
    assert issues.items[0].status == "failed"
    assert (await auto_queue_pending_summary(db_session, None)).pending_count == 1


async def test_virtual_batch_sql_on_postgres(db_session, printer_factory, archive_factory, monkeypatch):
    await assert_batch_sql(db_session, printer_factory, archive_factory, monkeypatch)


@pytest.mark.asyncio
async def test_virtual_batch_chunk_boundary_on_postgres(db_session, archive_factory, monkeypatch):
    printers = [
        Printer(
            name=f"Batch {index}",
            serial_number=f"00M09B{index:09d}",
            ip_address=f"192.0.2.{index % 254 + 1}",
            access_code="12345678",
            is_active=True,
            model="X1C",
        )
        for index in range(401)
    ]
    db_session.add_all(printers)
    await db_session.flush()
    db_session.add_all(PrinterQueue(id=printer.id, printer_id=printer.id) for printer in printers)
    archive = await archive_factory(printers[0].id, status="printing")
    await db_session.commit()
    bind_print_run(queue_virtual.printer_manager, printer_id=printers[0].id, archive_id=archive.id)
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda _id: SimpleNamespace(connected=True, state="RUNNING"),
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _id: None)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        rows = await queue_virtual.build_virtual_current_prints(db_session)
        assert [row["archive_id"] for row in rows] == [archive.id]
        assert len(statements) == 4, statements  # queue map, two real-row chunks, archive
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)
        discard_print_run(queue_virtual.printer_manager, printers[0].id, archive.id)


async def test_picker_profile_qualifier_on_postgres(db_session):
    spool = Spool(
        material="ABS",
        brand="Test",
        color_name="Blue",
        slicer_filament_name="Test_1   @H2D",
        label_weight=1000,
        core_weight=250,
    )
    db_session.add(spool)
    await db_session.commit()
    result = await spool_picker(
        printer_id=1,
        ams_id=0,
        tray_id=0,
        tray_profile="Test_1",
        tray_material="PETG",
        q="",
        show_all=False,
        replacing_spool_id=None,
        page=1,
        per_page=50,
        db=db_session,
        _=None,
    )
    assert [item.id for item in result.items] == [spool.id]
