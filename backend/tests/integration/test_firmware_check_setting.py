"""With "Check printer firmware" off, a printer card's check contacts nobody.

The printers page asks before its settings have loaded, so the check used to go
out to Bambu Lab once per printer on every page load even when switched off.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.models.settings import Settings


def _service():
    service = MagicMock()
    service.check_for_update = AsyncMock(
        return_value={
            "latest_version": "01.09.00.00",
            "update_available": True,
            "download_url": None,
            "release_notes": None,
            "available_versions": [],
        }
    )
    return service


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_card_check_contacts_nobody_when_the_setting_is_off(
    async_client: AsyncClient, printer_factory, db_session
):
    printer = await printer_factory(name="P", model="P1S")
    db_session.add(Settings(key="check_printer_firmware", value="false"))
    await db_session.commit()
    service = _service()

    with patch("backend.app.api.routes.firmware.get_firmware_service", return_value=service):
        response = await async_client.get(f"/api/v1/firmware/updates/{printer.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["update_available"] is False
    assert body["latest_version"] is None
    service.check_for_update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_card_check_runs_when_the_setting_is_on_or_unset(async_client: AsyncClient, printer_factory):
    printer = await printer_factory(name="P", model="P1S")
    service = _service()

    with patch("backend.app.api.routes.firmware.get_firmware_service", return_value=service):
        response = await async_client.get(f"/api/v1/firmware/updates/{printer.id}")

    assert response.status_code == 200
    assert response.json()["update_available"] is True
    service.check_for_update.assert_called_once()
