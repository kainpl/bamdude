"""Load and Unload address an AMS-HT by its unit id, as BambuStudio does (upstream 5a9415e2e).

BambuStudio's ``MachineObject::command_ams_change_filament``: a global tray is
``ams_id * 4 + slot`` only for a unit id below 16; for any other unit — an
AMS-HT's 128-135, the A2L AMS-Lite's physical 16 — the load ``target`` is the
unit id itself. An unload is ``target`` 255, ``slot_id`` 255 and the source
unit.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, change_filament_wire_ids


@pytest.fixture
def client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    c.model = "H2D"
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(client: BambuMQTTClient) -> dict:
    return json.loads(client._client.publish.call_args[0][1])["print"]


@pytest.mark.parametrize(
    ("tray_id", "expected"),
    [
        (0, (0, 0, 0)),
        (3, (0, 3, 3)),
        (5, (1, 1, 5)),
        (15, (3, 3, 15)),
        # An AMS-HT holds one tray: unit, slot 0, target = the unit.
        (128, (128, 0, 128)),
        (131, (131, 0, 131)),
        (135, (135, 0, 135)),
        # The A2L AMS-Lite (normalised unit 6 -> physical 16): target = the unit.
        (24, (16, 0, 16)),
        (27, (16, 3, 16)),
    ],
)
def test_the_wire_ids_of_a_load(tray_id, expected):
    assert change_filament_wire_ids(tray_id) == expected


def test_loading_an_ams_ht_tray_names_its_unit(client):
    assert client.ams_load_filament(129) is True

    sent = _published(client)
    assert (sent["command"], sent["ams_id"], sent["slot_id"], sent["target"]) == ("ams_change_filament", 129, 0, 129)


def test_loading_a_regular_slot_is_sent_as_before(client):
    assert client.ams_load_filament(6) is True

    sent = _published(client)
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == (1, 2, 6)
    assert client.state.pending_tray_target == 6


def test_loading_an_a2l_lite_slot_targets_the_unit(client):
    client.model = "A2L"
    assert client.ams_load_filament(25) is True

    sent = _published(client)
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == (16, 1, 16)


def test_an_ams_ht_load_leaves_no_pending_target(client):
    """The pending target is matched by ``target % 4``: 129 would read as slot 1 and
    the reported slot 0 would be written as AMS 0 slot 1. The hotend's own
    ``snow`` names an AMS-HT tray, so nothing has to be remembered for it."""
    client.state.pending_tray_target = None

    client.ams_load_filament(129)

    assert client.state.pending_tray_target is None


def test_unloading_an_ams_ht_tray_names_its_unit(client):
    client.state.tray_now = 255

    assert client.ams_unload_filament(130) is True

    sent = _published(client)
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == (130, 255, 255)


def test_unloading_an_ams_ht_tray_finds_the_hotend_that_holds_it(client):
    """HT-B is unit 129, slot 0 — not AMS 32 slot 1."""
    client._process_message(
        {
            "print": {
                "device": {
                    "extruder": {
                        "info": [
                            {"id": 0, "snow": (129 << 8) | 0, "info": 0b10},
                            {"id": 1, "snow": 0xFFFF, "info": 0},
                        ]
                    }
                }
            }
        }
    )
    assert client._is_dual_nozzle

    assert client.ams_unload_filament(129) is True
    assert _published(client)["ams_id"] == 129


def test_unloading_an_ams_ht_tray_no_hotend_holds_sends_nothing(client):
    client._process_message(
        {
            "print": {
                "device": {
                    "extruder": {
                        "info": [
                            {"id": 0, "snow": (0 << 8) | 1, "info": 0b10},
                            {"id": 1, "snow": 0xFFFF, "info": 0},
                        ]
                    }
                }
            }
        }
    )
    client._client.publish.reset_mock()

    assert client.ams_unload_filament(129) is False
    client._client.publish.assert_not_called()


async def _post(async_client, printer_id: int, client: BambuMQTTClient, url: str):
    from unittest.mock import AsyncMock, patch

    manager = MagicMock()
    manager.get_client.return_value = client
    manager.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
    with patch("backend.app.api.routes.printers.printer_manager", manager):
        return await async_client.post(f"/api/v1/printers/{printer_id}{url}")


@pytest.mark.asyncio
async def test_the_load_route_takes_an_ams_ht_unit(async_client, printer_factory, client):
    printer = await printer_factory(model="H2D")

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=128")

    assert response.status_code == 200, response.text
    assert _published(client)["target"] == 128


@pytest.mark.asyncio
@pytest.mark.parametrize("tray_id", [16, 23, 28, 127, 136, 253])
async def test_the_load_route_refuses_what_names_no_slot(async_client, printer_factory, client, tray_id):
    printer = await printer_factory(model="H2D")

    response = await _post(async_client, printer.id, client, f"/ams/load?tray_id={tray_id}")

    assert response.status_code == 400
    client._client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_the_unload_route_takes_an_ams_ht_unit(async_client, printer_factory, client):
    printer = await printer_factory(model="H2D")

    response = await _post(async_client, printer.id, client, "/ams/unload?tray_id=131")

    assert response.status_code == 200, response.text
    assert _published(client)["ams_id"] == 131
