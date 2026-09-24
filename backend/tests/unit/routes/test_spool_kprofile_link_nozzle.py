"""A spool's PA-tab link resolves the index inside the nozzle it was picked for.

The PA tab posts ``(printer, extruder, nozzle_diameter, cali_idx)`` and the
backend looks the index up in the printer's live table to build the cache row.
``cali_idx`` is numbered per nozzle, and the live table holds every nozzle's
entries once it is filed per diameter — matching on the index alone linked the
spool to whichever nozzle's entry came first.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.api.routes.inventory import _find_or_create_filament_calibration_for_link
from backend.app.schemas.spool import SpoolKProfileBase
from backend.app.services.bambu_mqtt import KProfile


def _live(cali_idx: int, k: str, nozzle: str, extruder: int = 0) -> KProfile:
    return KProfile(
        slot_id=cali_idx,
        extruder_id=extruder,
        nozzle_id=f"HS00-{nozzle}",
        nozzle_diameter=nozzle,
        filament_id="GFG96",
        name=f"PETG {nozzle} e{extruder}",
        k_value=k,
    )


async def _resolve(db, printer_id: int, live: list[KProfile], **link):
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = live
    with patch("backend.app.services.printer_manager.printer_manager.get_client", return_value=client):
        return await _find_or_create_filament_calibration_for_link(db, SpoolKProfileBase(printer_id=printer_id, **link))


@pytest.mark.asyncio
async def test_the_index_is_read_in_the_picked_nozzles_table(db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    live = [_live(2, "0.020", "0.4"), _live(2, "0.030", "0.6")]

    fc = await _resolve(db_session, printer.id, live, nozzle_diameter="0.6", k_value=0.03, cali_idx=2)

    assert fc is not None
    assert (fc.nozzle_diameter, fc.pa_k_value) == (0.6, 0.03)


@pytest.mark.asyncio
async def test_the_index_is_read_on_the_picked_hotend(db_session, printer_factory):
    printer = await printer_factory(model="H2D")
    live = [_live(2, "0.020", "0.4", extruder=0), _live(2, "0.018", "0.4", extruder=1)]

    fc = await _resolve(db_session, printer.id, live, extruder=1, nozzle_diameter="0.4", k_value=0.018, cali_idx=2)

    assert fc is not None
    assert (fc.extruder_id, fc.pa_k_value) == (1, 0.018)


@pytest.mark.asyncio
async def test_an_index_the_picked_nozzle_does_not_have_links_nothing(db_session, printer_factory):
    """Another nozzle's entry under the same index is a different profile."""
    printer = await printer_factory(model="X1C")
    live = [_live(2, "0.020", "0.4")]

    fc = await _resolve(db_session, printer.id, live, nozzle_diameter="0.6", k_value=0.02, cali_idx=2)

    assert fc is None
