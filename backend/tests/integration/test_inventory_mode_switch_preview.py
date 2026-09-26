"""What an inventory-mode switch would take away, asked before it happens (audit D4, upstream #2812).

Switching between the built-in inventory and Spoolman clears every slot
assignment of the mode being left — deliberately: the live tables hold only the
active mode, so no reader can be answered by a row of the other one. What made
it destroy an upstream user's configuration was the settings page saving the
switch on its own, 500 ms after a click, with nothing asked. The page now asks
first, and this is what it asks with: how many assignments go, and which
printers are printing right now (their filament will not be charged to either
inventory).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

URL = "/api/v1/settings/spoolman/mode-switch-preview"


async def _mode(db, spoolman: bool):
    db.add(Settings(key="spoolman_enabled", value="true" if spoolman else "false"))
    await db.commit()


async def _internal_assignments(db, printer, n):
    for tray in range(n):
        spool = Spool(material="PLA")
        db.add(spool)
        await db.flush()
        db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=tray))
    await db.commit()


async def test_switching_to_spoolman_names_the_internal_assignments(async_client, db_session, printer_factory):
    printer = await printer_factory()
    await _mode(db_session, spoolman=False)
    await _internal_assignments(db_session, printer, 3)

    response = await async_client.get(URL, params={"enable": "true"})

    assert response.status_code == 200, response.text
    assert response.json() == {"assignments": 3, "printing": []}


async def test_switching_back_names_the_spoolman_assignments(async_client, db_session, printer_factory):
    printer = await printer_factory()
    await _mode(db_session, spoolman=True)
    db_session.add_all(
        [
            SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=11),
            SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=12),
        ]
    )
    await db_session.commit()

    response = await async_client.get(URL, params={"enable": "false"})

    assert response.json()["assignments"] == 2


async def test_asking_for_the_mode_already_on_takes_nothing(async_client, db_session, printer_factory):
    printer = await printer_factory()
    await _mode(db_session, spoolman=False)
    await _internal_assignments(db_session, printer, 2)

    response = await async_client.get(URL, params={"enable": "false"})

    assert response.json() == {"assignments": 0, "printing": []}


async def test_printers_printing_now_are_named(async_client, db_session, printer_factory):
    busy = await printer_factory(name="Busy X1C")
    await printer_factory(name="Idle P1S")
    await _mode(db_session, spoolman=False)

    with patch(
        "backend.app.services.printer_manager.printer_manager.is_print_active",
        side_effect=lambda pid: pid == busy.id,
    ):
        response = await async_client.get(URL, params={"enable": "true"})

    assert response.json()["printing"] == ["Busy X1C"]
