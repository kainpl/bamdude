"""The status callback feeds WLED (N02 A4): after the session proof, and never at the callback's expense."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app import main
from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import PrinterState

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PRINTER_ID = 428


@pytest.fixture
async def callback(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    async with factory.begin() as db:
        db.add(
            Printer(
                id=PRINTER_ID, name="WLED", serial_number="test-wled", ip_address="localhost", access_code="00000000"
            )
        )
    for store in (
        main._last_status_broadcast,
        main._last_printer_state,
        main._last_printer_state_generation,
        main._printer_last_connected,
        main._projection_repeat_available,
    ):
        monkeypatch.setitem(store, PRINTER_ID, None)
    monkeypatch.setattr(main, "_broadcast_printer_status_projection", AsyncMock())
    repeat = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "_repeat_available_for", repeat)
    monkeypatch.setattr(main, "_capture_snapshot_for_notification", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "notification_service", AsyncMock())
    monkeypatch.setattr(main.mqtt_relay, "on_printer_status", AsyncMock(), raising=False)
    handle = MagicMock()
    monkeypatch.setattr(main.wled_manager, "handle_status", handle)
    return handle, repeat


async def test_the_status_callback_feeds_wled_after_the_session_proof(callback, monkeypatch):
    handle, _ = callback
    state = PrinterState(connected=True, state="IDLE")
    await main.on_printer_status_change(PRINTER_ID, state)
    assert handle.call_count == 1
    assert handle.call_args.args[0] == PRINTER_ID
    assert handle.call_args.kwargs == {"awaiting_plate_clear": False}

    handle.reset_mock()
    monkeypatch.setattr(main.printer_manager, "session_still_lives", lambda printer_id: False)
    await main.on_printer_status_change(PRINTER_ID, PrinterState(connected=True, state="RUNNING"))
    handle.assert_not_called()


async def test_a_wled_failure_does_not_fail_the_status_callback(callback):
    """Review Focus 3: a WLED bug never stops what follows in the callback."""
    handle, repeat = callback
    handle.side_effect = RuntimeError("wled broke")
    await main.on_printer_status_change(PRINTER_ID, PrinterState(connected=True, state="IDLE"))
    assert handle.call_count == 1
    repeat.assert_awaited()
