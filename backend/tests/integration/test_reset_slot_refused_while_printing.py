"""Reset Slot clears a slot's filament setting; under a job that is the print's own filament."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _manager(mock_pm, client):
    mock_pm.ensure_fresh_connection = AsyncMock(return_value=True)
    mock_pm.get_client.return_value = client


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reset_is_refused_while_a_job_is_on_the_printer(async_client: AsyncClient, printer_factory):
    printer = await printer_factory(name="Busy")
    client = MagicMock()

    with (
        patch("backend.app.api.routes.printers.printer_manager") as mock_pm,
        patch("backend.app.api.routes.printers.is_printer_busy", return_value=True),
    ):
        _manager(mock_pm, client)
        response = await async_client.post(f"/api/v1/printers/{printer.id}/ams/0/tray/1/reset")

    assert response.status_code == 409
    client.reset_ams_slot.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reset_goes_out_on_an_idle_printer(async_client: AsyncClient, printer_factory):
    printer = await printer_factory(name="Idle")
    client = MagicMock()
    client.reset_ams_slot.return_value = True

    with (
        patch("backend.app.api.routes.printers.printer_manager") as mock_pm,
        patch("backend.app.api.routes.printers.is_printer_busy", return_value=False),
    ):
        _manager(mock_pm, client)
        response = await async_client.post(f"/api/v1/printers/{printer.id}/ams/0/tray/1/reset")

    assert response.status_code == 200
    client.reset_ams_slot.assert_called_once_with(ams_id=0, tray_id=1)
