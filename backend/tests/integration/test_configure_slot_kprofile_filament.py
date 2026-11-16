"""Configure Slot addresses a K-profile by a FILAMENT id (upstream d6de7aa69·2).

``kprofile_filament_id`` went to the printer as given. An API client that sent
a Bambu setting id there ("GFSL99") addressed the profile under an id the
printer's K table does not use — the table is keyed by filament id ("GFL99").
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PARAMS = {
    "tray_info_idx": "GFL99",
    "tray_type": "PLA",
    "tray_sub_brands": "Generic PLA",
    "tray_color": "FFFFFFFF",
    "nozzle_temp_min": 190,
    "nozzle_temp_max": 230,
}


async def _configure(async_client, printer, **extra) -> MagicMock:
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    client.extrusion_cali_sel.return_value = True
    client.extrusion_cali_set.return_value = True
    with patch("backend.app.api.routes.printers.printer_manager") as manager:
        manager.ensure_fresh_connection = AsyncMock(return_value=True)
        manager.get_client.return_value = client
        manager.get_status.return_value = None
        manager.get_model.return_value = "P1S"
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/slots/0/1/configure", params={**_PARAMS, **extra}
        )
    assert response.status_code == 200, response.text
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("GFSL99", "GFL99"),  # a bare setting id
        ("GFSA05_07", "GFA05"),  # a versioned one keeps its family (Silk, not Matte)
        ("GFL99", "GFL99"),  # a filament id is left alone
        ("P122e532", "P122e532"),  # a user preset's own id is left alone
    ],
)
async def test_the_selection_names_the_filament_id(async_client, printer_factory, given, sent):
    printer = await printer_factory(name="P1S", model="P1S")
    client = await _configure(async_client, printer, cali_idx=4, kprofile_filament_id=given)
    assert client.extrusion_cali_sel.call_args.kwargs["filament_id"] == sent


@pytest.mark.asyncio
async def test_a_typed_k_value_is_filed_under_the_filament_id(async_client, printer_factory):
    printer = await printer_factory(name="P1S", model="P1S")
    client = await _configure(async_client, printer, k_value=0.031, kprofile_filament_id="GFSL99")
    assert client.extrusion_cali_set.call_args.kwargs["filament_id"] == "GFL99"
