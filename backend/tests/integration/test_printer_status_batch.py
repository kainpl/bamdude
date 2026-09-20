"""Fleet REST fallback has exact single-status semantics without N DB reads."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import event

from backend.app.api.routes.printers import get_printer_status, get_printer_status_batch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.bambu_mqtt import PrinterState


@pytest.mark.asyncio
async def test_fifty_statuses_match_single_reads_with_bounded_sql(printer_factory, archive_factory, db_session):
    printers = [await printer_factory(name=f"Fleet {i}") for i in range(50)]
    states = {}
    # Archive timestamps use naive UTC on both supported databases.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, printer in enumerate(printers):
        states[printer.id] = PrinterState()
        states[printer.id].state = "PAUSE" if i % 2 else "RUNNING"
        states[printer.id].connected = True
        states[printer.id].subtask_id = f"cloud-{i}" if i % 2 else ""
        await archive_factory(printer.id, status="printing", created_at=now)
        if i % 2:
            # Explicit cloud subtask beats a newer open archive.
            await archive_factory(printer.id, subtask_id=f"cloud-{i}", created_at=now - timedelta(days=1))
    queue = PrinterQueue(printer_id=printers[0].id)
    db_session.add(queue)
    await db_session.flush()
    db_session.add(PrintQueueItem(queue_id=queue.id, status="completed"))
    await db_session.commit()
    engine = db_session.bind.sync_engine
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.get_status.side_effect = states.get
        pm.is_awaiting_plate_clear.side_effect = lambda pid: pid == printers[0].id
        expected = {p.id: (await get_printer_status(p.id, None, db_session)).model_dump() for p in printers}
        event.listen(engine, "before_cursor_execute", record)
        try:
            actual = await get_printer_status_batch([p.id for p in printers], None, db_session)
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert {pid: row.model_dump() for pid, row in actual.items()} == expected
    assert len(statements) <= 5, f"Status reads must stay bounded for 50 printers: {len(statements)}"
    assert actual[printers[0].id].repeat_available
    assert all(actual[p.id].current_archive_id is not None for p in printers)


@pytest.mark.asyncio
async def test_batch_route_validates_ids_auth_and_preserves_missing_offline_states(async_client, printer_factory):
    printer = await printer_factory()
    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.get_status.return_value = None
        pm.is_awaiting_plate_clear.return_value = False
        response = await async_client.get(
            "/api/v1/printers/status/batch", params=[("ids", printer.id), ("ids", 999999)]
        )
    assert response.status_code == 200, response.text
    assert list(response.json()) == [str(printer.id)]
    assert response.json()[str(printer.id)]["connected"] is False
    assert (await async_client.get("/api/v1/printers/status/batch")).status_code == 422
    assert (
        await async_client.get("/api/v1/printers/status/batch", params=[("ids", i) for i in range(101)])
    ).status_code == 422
    unauthorized = await async_client.get("/api/v1/printers/status/batch?ids=1", headers={"Authorization": ""})
    assert unauthorized.status_code in (401, 403)
