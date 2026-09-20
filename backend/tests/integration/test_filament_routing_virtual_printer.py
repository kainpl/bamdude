"""VP writers and late MQTT options use the same durable routing rules."""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.virtual_printer.manager import VirtualPrinterInstance
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["print_queue", "auto_queue"])
async def test_virtual_printer_intake_and_late_options_preserve_exact_plate_and_feed(
    db_session,
    test_engine,
    tmp_path,
    printer_factory,
    monkeypatch,
    mode,
):
    source, printer, queue, _ = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source.file_metadata = {"sliced_for_model": "P1P", "plates": [{"index": 15}]}
    await db_session.commit()
    vp = VirtualPrinterInstance(
        vp_id=1,
        name="Synthetic",
        mode=mode,
        model="C11",
        access_code="00000000",
        serial_suffix="000000001",
        target_printer_id=printer.id,
        auto_dispatch=True,
        base_dir=tmp_path,
        session_factory=async_sessionmaker(test_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(vp, "_save_to_library", AsyncMock(return_value=source))
    monkeypatch.setattr(vp, "_find_best_queue", AsyncMock(return_value=queue))
    filename = Path(source.file_path).name
    vp._slicer_print_options[filename] = {"use_ams": True}
    if mode == "print_queue":
        await vp._add_to_print_queue(Path(source.file_path), "127.0.0.1")
        row = (await db_session.execute(select(PrintQueueItem))).scalar_one()
        rules = json.loads(row.filament_routing)
        assert rules["feed_policy"] == "auto" and rules["mode"] == "auto"
        restamp, recent = vp._restamp_recent_queue_item, vp._recent_queue_items
    else:
        await vp._add_to_auto_queue(Path(source.file_path), "127.0.0.1")
        row = (await db_session.execute(select(AutoQueueItem))).scalar_one()
        assert row.feed_policy == "auto" and row.force_color_match is False
        restamp, recent = vp._restamp_recent_auto_item, vp._recent_auto_items
    assert row.plate_id == 15
    recent[filename] = ([row.id], time.monotonic())
    await restamp(filename, {"use_ams": False})
    await db_session.refresh(row)
    assert row.use_ams is False
    assert (
        json.loads(row.filament_routing)["feed_policy"] if mode == "print_queue" else row.feed_policy
    ) == "external_only"
    row.status = "printing" if mode == "print_queue" else "assigned"
    await db_session.commit()
    recent[filename] = ([row.id], time.monotonic())
    await restamp(filename, {"use_ams": True})
    await db_session.refresh(row)
    assert row.use_ams is False
