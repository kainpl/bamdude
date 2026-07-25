"""P1-series AMS drying is screen-only — the API must refuse it (upstream #2533).

Bambu's P1 manual states that "P1S connected AMS drying functions may only be
controlled from the P1S screen". The firmware still answers
``ams_filament_drying`` with ``result: success`` and then ignores it, so a
command we can't fulfil must be refused rather than acked — and that has to hold
for stop as well as start, since a cycle a P1S user started at the printer can
only be ended there.

BamDude additionally covers the internal SSDP codes (C11=P1P, C12=P1S) because
``Printer.model`` may hold either form: the Discovery-add path in the UI maps the
code to the display name before the POST, but a manually-added or API-created
printer persists whatever it was given.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

SCREEN_ONLY_DETAIL = "This printer only supports AMS drying from its own screen"


@pytest.fixture(autouse=True)
def _connection_probe():
    """Add-Printer probes MQTT before persisting; default it to success."""
    with patch(
        "backend.app.api.routes.printers.printer_manager.test_connection",
        new=AsyncMock(return_value={"success": True, "state": "IDLE", "model": "P1S"}),
    ):
        yield


@pytest.fixture
def mqtt_send():
    """Watch the MQTT command so we can assert nothing was published."""
    with patch(
        "backend.app.services.printer_manager.printer_manager.send_drying_command",
        new=MagicMock(return_value=True),
    ) as m:
        yield m


@pytest.fixture
def live_state():
    """A connected printer on firmware new enough that only the model gates drying."""
    state = MagicMock()
    state.firmware_version = "01.10.00.00"
    state.raw_data = {"ams": [{"id": 0, "module_type": "n3f", "tray": []}]}
    with patch(
        "backend.app.api.routes.printers.printer_manager.get_status",
        new=MagicMock(return_value=state),
    ):
        yield state


async def _add_printer(async_client: AsyncClient, model: str, serial: str) -> int:
    resp = await async_client.post(
        "/api/v1/printers/",
        json={
            "name": f"{model} test",
            "serial_number": serial,
            "ip_address": "192.168.1.77",
            "access_code": "12345678",
            "is_active": True,
            "model": model,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "model,serial",
    [("P1S", "01P00A000000001"), ("P1P", "01P00A000000002"), ("C12", "01P00A000000003")],
)
async def test_start_drying_refused_on_screen_only_models(
    async_client: AsyncClient, mqtt_send, live_state, model, serial
):
    printer_id = await _add_printer(async_client, model, serial)

    resp = await async_client.post(f"/api/v1/printers/{printer_id}/drying/start?ams_id=0&temp=55&duration=6")

    assert resp.status_code == 400
    assert SCREEN_ONLY_DETAIL in resp.json()["detail"]
    # The whole point: nothing reached the printer.
    mqtt_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_drying_refused_on_screen_only_models(async_client: AsyncClient, mqtt_send, live_state):
    """Stop is refused too — a P1 ignores it exactly as it ignores start, so
    reporting success would be a lie about a cycle that keeps running."""
    printer_id = await _add_printer(async_client, "P1S", "01P00A000000004")

    resp = await async_client.post(f"/api/v1/printers/{printer_id}/drying/stop?ams_id=0")

    assert resp.status_code == 400
    assert SCREEN_ONLY_DETAIL in resp.json()["detail"]
    mqtt_send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stop_drying_still_works_on_a_normal_model(async_client: AsyncClient, mqtt_send, live_state):
    """Guard against the gate being too wide: an X1C must still be stoppable."""
    printer_id = await _add_printer(async_client, "X1C", "00M09A000000005")

    resp = await async_client.post(f"/api/v1/printers/{printer_id}/drying/stop?ams_id=0")

    assert resp.status_code == 200
    assert resp.json()["status"] == "drying_stopped"
    mqtt_send.assert_called_once()
