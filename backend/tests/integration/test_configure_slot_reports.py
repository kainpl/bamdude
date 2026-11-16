"""Configure Slot says what did NOT reach the printer.

The route used to answer ``success`` once the filament setting was published,
whatever became of the K half: the result of the ``extrusion_cali_sel`` /
``extrusion_cali_set`` sends was dropped (upstream ab5ccc108), so an operator
who typed a K value was told "Configured" when the K never left.
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


def _client(*, sel=True, cali_set=True) -> MagicMock:
    client = MagicMock()
    client.ams_set_filament_setting.return_value = True
    client.extrusion_cali_sel.return_value = sel
    client.extrusion_cali_set.return_value = cali_set
    client.request_status_update.return_value = True
    return client


async def _configure(async_client, printer, client, **extra):
    with patch("backend.app.api.routes.printers.printer_manager") as manager:
        manager.ensure_fresh_connection = AsyncMock(return_value=True)
        manager.get_client.return_value = client
        manager.get_status.return_value = None
        manager.get_model.return_value = "P1S"
        return await async_client.post(
            f"/api/v1/printers/{printer.id}/slots/0/1/configure", params={**_PARAMS, **extra}
        )


@pytest.mark.asyncio
async def test_a_typed_k_value_that_was_not_sent_is_reported(async_client, printer_factory):
    printer = await printer_factory(name="P1S", model="P1S")
    client = _client(cali_set=False)

    response = await _configure(async_client, printer, client, k_value=0.031)

    assert response.status_code == 200, response.text
    client.extrusion_cali_set.assert_called_once()
    assert response.json()["warnings"] == ["k_value_not_sent"]


@pytest.mark.asyncio
async def test_a_k_selection_that_was_not_sent_is_reported(async_client, printer_factory):
    printer = await printer_factory(name="P1S", model="P1S")
    client = _client(sel=False)

    response = await _configure(async_client, printer, client, cali_idx=4)

    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == ["k_profile_not_sent"]


@pytest.mark.asyncio
async def test_a_slot_whose_k_went_out_has_no_warnings(async_client, printer_factory):
    printer = await printer_factory(name="P1S", model="P1S")
    client = _client()

    response = await _configure(async_client, printer, client, k_value=0.031)

    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == []
