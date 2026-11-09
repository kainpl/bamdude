"""First PAUSE enters the real journal seed before the repeated HMS can fire."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app import main
from backend.app.models.print_usage_event import PrintUsageEvent
from backend.app.services import print_usage_journal as journal
from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_downtime_runout", "closed_on_paper", "journal_error"])
async def test_first_pause_preserves_existing_seed_policy(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, case
):
    p = await printer_factory()
    a = await archive_factory(printer_id=p.id, status="printing", subtask_id="100")
    if case == "closed_on_paper":
        db_session.add_all(
            [
                PrintUsageEvent(
                    printer_id=p.id, archive_id=a.id, layer_num=3, event="runout", kind="pause", global_tray_id=0
                ),
                PrintUsageEvent(
                    printer_id=p.id,
                    archive_id=a.id,
                    layer_num=3,
                    event="spool_loaded",
                    global_tray_id=0,
                    slot_occupied=False,
                ),
            ]
        )
    await db_session.commit()
    c = BambuMQTTClient(ip_address="192.0.2.1", serial_number="PAUSE-SEED", access_code="test")
    c.on_print_running_observed = MagicMock()
    c.on_usage_event = MagicMock()
    c.state.tray_now = 0
    c.state.raw_data = {"ams": {"ams": [{"id": 0, "tray": [{"id": 0, "tray_uuid": "A" * 32}]}]}}
    signal = {"hms": [{"attr": 0x07002000, "code": 0x00020001}]}
    c._process_message(
        {"print": {"gcode_state": "PAUSE", "gcode_file": "job.3mf", "subtask_id": "100", "layer_num": 8, **signal}}
    )
    c.on_usage_event.assert_not_called()
    monkeypatch.setattr(main.printer_manager, "get_client", lambda _pid: c)
    monkeypatch.setattr(main.printer_manager, "get_status", lambda _pid: c.state)
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    if case == "journal_error":
        monkeypatch.setattr(journal, "load_events", AsyncMock(side_effect=RuntimeError("journal unavailable")))
    await main._seed_runout_detector_from_journal(p.id, logging.getLogger(__name__))
    assert c._runout_seeded is True
    c._process_message({"print": signal})
    c._process_message({"print": signal})
    if case == "closed_on_paper":
        c.on_usage_event.assert_not_called()
    else:
        c.on_usage_event.assert_called_once_with("runout", "pause", 0, 8)
