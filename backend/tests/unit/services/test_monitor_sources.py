"""Typed waits survive reads, but never a queue lifecycle change."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import import_all_models
from backend.app.migrations.m170_monitor_wait_reasons import upgrade
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.printer_manager import PrinterManager
from backend.app.services.queue_wait_reason import set_wait_reason

import_all_models()


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "printing"),
        ("status", "cancelled"),
        ("position", 2),
        ("manual_start", True),
        ("queue_id", 2),
        ("waiting_reason", "A different legacy message"),
    ],
)
def test_lifecycle_edit_invalidates_recorded_wait(field, value):
    item = PrintQueueItem(queue_id=1, status="pending", position=1, manual_start=False)
    assert set_wait_reason(item, "filament_unavailable", "Needs material")
    assert item.waiting_reason_checked_at is not None
    assert not set_wait_reason(item, "filament_unavailable", "Needs material")
    setattr(item, field, value)
    assert item.waiting_reason_code is None and item.waiting_reason_checked_at is None
    if field != "waiting_reason":
        assert item.waiting_reason is None


def test_clear_wait_removes_all_metadata():
    item = PrintQueueItem(queue_id=1, status="pending")
    set_wait_reason(item, "stagger", "Wait for a slot")
    set_wait_reason(item, None, None)
    assert item.waiting_reason is None and item.waiting_reason_code is None and item.waiting_reason_checked_at is None


def test_monitor_read_never_calls_reconnection():
    manager = PrinterManager()
    client = SimpleNamespace(
        state=object(),
        status_received_at=123.0,
        is_stale=Mock(return_value=True),
        check_staleness=Mock(side_effect=AssertionError("must not reconnect")),
    )
    manager._clients[1] = client
    assert manager.peek_status(1) == (client.state, 123.0, True)
    assert manager.peek_status(2) == (None, None, False)
    client.check_staleness.assert_not_called()


async def test_upgrade_retains_legacy_prose_without_inventing_a_code():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, waiting_reason TEXT)"))
            await conn.execute(text("INSERT INTO print_queue VALUES (1, 'Printer offline')"))
            await upgrade(conn)
            await upgrade(conn)
            row = (await conn.execute(text("SELECT * FROM print_queue"))).mappings().one()
            assert row["waiting_reason"] == "Printer offline"
            assert row["waiting_reason_code"] is None and row["waiting_reason_checked_at"] is None
    finally:
        await engine.dispose()
