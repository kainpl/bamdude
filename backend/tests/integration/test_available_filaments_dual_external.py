"""A dual-nozzle printer with no AMS keeps its two external spools apart (upstream 113f08a1a leg 4).

The extruder of an external holder was taken only when the AMS extruder map
was non-empty — and without an AMS it is empty, so both holders answered
``None`` and two identical spools on different hotends merged into one.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient


def _status(vt_tray: list) -> MagicMock:
    status = MagicMock()
    status.raw_data = {"ams": [], "vt_tray": vt_tray, "ams_extruder_map": {}}
    return status


_BLACK_PLA = {"tray_type": "PLA", "tray_color": "000000FF", "tray_info_idx": "GFL99", "tray_sub_brands": "PLA Basic"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_identical_external_spools_on_two_hotends_stay_two(async_client: AsyncClient, printer_factory):
    await printer_factory(name="Dual", model="H2D")
    status = _status([{"id": 254, **_BLACK_PLA}, {"id": 255, **_BLACK_PLA}])

    with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = status
        response = await async_client.get("/api/v1/printers/available-filaments?model=H2D")

    assert response.status_code == 200
    assert sorted(entry["extruder_id"] for entry in response.json()) == [0, 1]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_single_nozzle_printer_without_an_ams_still_names_no_extruder(
    async_client: AsyncClient, printer_factory
):
    await printer_factory(name="Single", model="P1S")
    status = _status([{"id": 254, **_BLACK_PLA}])

    with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = status
        response = await async_client.get("/api/v1/printers/available-filaments?model=P1S")

    assert response.status_code == 200
    assert [entry["extruder_id"] for entry in response.json()] == [None]
