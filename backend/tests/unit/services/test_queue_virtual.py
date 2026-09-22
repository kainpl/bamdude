from types import SimpleNamespace

import pytest

from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_virtual
from backend.app.services.print_run_binding import bind_print_run, discard_print_run


@pytest.mark.asyncio
async def test_virtual_current_print_prefers_run_binding_over_filename_alias(
    db_session, printer_factory, archive_factory, monkeypatch
):
    """The UI must not show stale A merely because an alias still names it."""

    from backend.app import main

    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    stale = await archive_factory(printer.id, status="printing", print_name="Repeat")
    current = await archive_factory(printer.id, status="printing", print_name="Repeat")
    await db_session.commit()

    main._active_prints[(printer.id, "Repeat.3mf")] = stale.id
    binding = bind_print_run(queue_virtual.printer_manager, printer_id=printer.id, archive_id=current.id)
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda _printer_id: SimpleNamespace(connected=True, state="RUNNING"),
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _printer_id: None)
    try:
        virtual = await queue_virtual.build_virtual_current_print(db_session, printer.id)
        assert virtual is not None
        assert virtual["archive_id"] == current.id
    finally:
        discard_print_run(queue_virtual.printer_manager, printer.id, binding.archive_id)
        main._active_prints.pop((printer.id, "Repeat.3mf"), None)
