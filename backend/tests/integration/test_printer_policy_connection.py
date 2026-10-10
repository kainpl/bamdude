"""Policy edits keep the live MQTT insertion baseline; actual connection edits reconnect."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.tests.unit.services.test_auto_stock_spool import GROUP

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("policy", ["auto_stock_spool", "backup_compatibility"])
async def test_form_echoing_unchanged_connection_fields_does_not_reconnect(async_client, printer_factory, policy):
    printer = await printer_factory()
    value = {"enabled": True, "group": GROUP} if policy == "auto_stock_spool" else {"normalize_color": True}
    payload = {
        "ip_address": printer.ip_address,
        "is_active": printer.is_active,
        "access_code": printer.access_code,
        "ams_policies": {policy: value},
    }
    with patch("backend.app.api.routes.printers.printer_manager") as manager:
        manager.connect_printer = AsyncMock(return_value=True)
        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json=payload)
    assert response.status_code == 200
    manager.disconnect_printer.assert_not_called()
    manager.connect_printer.assert_not_awaited()
    saved = response.json()["ams_policies"][policy]
    if policy == "auto_stock_spool":
        assert saved == value
    else:
        assert saved["normalize_color"] is True


@pytest.mark.parametrize(
    "field,value", [("ip_address", "192.0.2.25"), ("access_code", "new-fixture"), ("is_active", False)]
)
async def test_changed_connection_settings_still_reconnect(async_client, printer_factory, field, value):
    printer = await printer_factory(is_active=True)
    with patch("backend.app.api.routes.printers.printer_manager") as manager:
        manager.connect_printer = AsyncMock(return_value=True)
        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={field: value})
    assert response.status_code == 200
    manager.disconnect_printer.assert_called_once_with(printer.id)
    if field == "is_active":
        manager.connect_printer.assert_not_awaited()
    else:
        manager.connect_printer.assert_awaited_once()
