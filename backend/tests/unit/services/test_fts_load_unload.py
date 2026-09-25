"""Load and Unload on a dual-nozzle printer, with and without a Filament Track Switch (upstream 9500c046).

With a switch fitted every AMS sits on a switch inlet and reaches both hotends,
so ``ams_change_filament`` must name the hotend (``extruder_id``) or the
firmware drops it — BambuStudio asks (``FeedDirectionDialog``) and sends the
field only then (``DeviceManager::command_ams_change_filament``). Unload was
aimed with the printer-wide ``tray_now``; BambuStudio addresses the slot's AMS
and sends nothing unless some hotend is fed from that slot
(``StatusPanel::on_ams_unload``). Which hotend holds which slot is
``device.extruder.info[i].snow`` (bits 8-15 AMS, 0-7 slot) plus ``info`` bit 1
"has filament" (``DevExtruderSystem::ParseExtruderInfo``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

FTS_AUX = "20000000"  # aux bit 29


def _snow(ams_id: int, slot: int) -> int:
    return (ams_id << 8) | slot


def _info(extruder: int, inlet_bits: int = 0xF) -> str:
    return format((inlet_bits << 24) | (extruder << 8) | 1, "08X")


@pytest.fixture
def client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    c.model = "H2C"
    c._client = MagicMock()
    c.state.connected = True
    return c


def _extruders(client: BambuMQTTClient, *entries: dict) -> None:
    client._process_message({"print": {"device": {"extruder": {"info": list(entries)}}}})


def _published(client: BambuMQTTClient) -> dict:
    return json.loads(client._client.publish.call_args[0][1])["print"]


# ---------------------------------------------------------------- the holders


def test_each_hotend_reports_the_slot_it_is_fed_from(client):
    _extruders(
        client,
        {"id": 0, "snow": _snow(0, 2), "info": 0b10},
        {"id": 1, "snow": _snow(1, 3), "info": 0},
    )

    right, left = client.state.extruder_slots[0], client.state.extruder_slots[1]
    assert (right.ams_id, right.slot_id, right.has_filament) == (0, 2, True)
    assert (left.ams_id, left.slot_id, left.has_filament) == (1, 3, False)


def test_the_empty_sentinel_is_no_slot(client):
    _extruders(client, {"id": 0, "snow": 0xFFFF, "info": 0}, {"id": 1, "snow": _snow(0, 0), "info": 2})

    assert client.state.extruder_slots[0].ams_id is None
    assert client.state.extruder_slots[0].slot_id is None


def test_a_frame_without_the_block_keeps_the_last_answer(client):
    """A temperature-only push must not read as "both hotends are now empty"."""
    _extruders(client, {"id": 0, "snow": _snow(0, 2), "info": 2}, {"id": 1, "snow": _snow(1, 0), "info": 2})
    client._process_message({"print": {"bed_temper": 60}})

    assert client.state.extruder_slots[0].ams_id == 0


# ---------------------------------------------------------------------- load


def test_a_load_names_the_hotend_only_when_asked(client):
    client.ams_load_filament(6, extruder_id=1)
    assert _published(client)["extruder_id"] == 1

    client.ams_load_filament(6)
    assert "extruder_id" not in _published(client)


# -------------------------------------------------------------------- unload


def test_an_addressed_unload_targets_the_slots_ams_not_tray_now(client):
    _extruders(client, {"id": 0, "snow": _snow(0, 1), "info": 2}, {"id": 1, "snow": _snow(1, 2), "info": 2})
    client.state.tray_now = 1  # printer-wide field names the right hotend's slot

    assert client.ams_unload_filament(6) is True  # AMS 1 slot 2 — the left hotend's

    sent = _published(client)
    assert (sent["ams_id"], sent["slot_id"], sent["target"]) == (1, 255, 255)


def test_an_addressed_unload_of_a_slot_no_hotend_holds_sends_nothing(client):
    _extruders(client, {"id": 0, "snow": _snow(0, 1), "info": 2}, {"id": 1, "snow": 0xFFFF, "info": 0})

    assert client.ams_unload_filament(7) is False
    client._client.publish.assert_not_called()


def test_a_single_nozzle_printer_keeps_its_unload(client):
    """One hotend: tray_now names the loaded slot exactly, and nobody has read a
    single-nozzle ``snow`` off the wire — the holder check stays off."""
    client.model = "X1C"
    _extruders(client, {"id": 0, "snow": _snow(0, 0), "info": 2})
    client.state.tray_now = 3

    assert client.ams_unload_filament(3) is True
    assert _published(client)["ams_id"] == 0


def test_an_unaddressed_unload_still_follows_tray_now(client):
    client.state.tray_now = 5

    assert client.ams_unload_filament() is True
    assert _published(client)["ams_id"] == 1


# -------------------------------------------------------------- the payloads


def test_the_holders_travel_on_the_socket_and_move_the_broadcast_key(client):
    from backend.app.main import _fts_status_key
    from backend.app.services.printer_manager import printer_state_to_dict

    _extruders(client, {"id": 0, "snow": _snow(0, 1), "info": 2}, {"id": 1, "snow": 0xFFFF, "info": 0})
    before = _fts_status_key(client.state)
    payload = printer_state_to_dict(client.state, 1, "H2C")
    assert payload["extruder_slots"] == {
        "0": {"ams_id": 0, "slot_id": 1, "has_filament": True},
        "1": {"ams_id": None, "slot_id": None, "has_filament": False},
    }

    _extruders(client, {"id": 0, "snow": _snow(0, 1), "info": 2}, {"id": 1, "snow": _snow(1, 0), "info": 2})
    assert _fts_status_key(client.state) != before


# -------------------------------------------------------------------- routes


def _fts_frame(client: BambuMQTTClient, *units: tuple[str, str]) -> None:
    client._process_message(
        {
            "print": {
                "aux": FTS_AUX,
                "device": {"fila_switch": {"in": [-1, -1], "out": [0, 1]}},
                "ams": {"ams": [{"id": ams_id, "info": info, "tray": []} for ams_id, info in units]},
            }
        }
    )


async def _post(async_client, printer_id: int, client: BambuMQTTClient, url: str):
    manager = MagicMock()
    manager.get_client.return_value = client
    manager.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
    with patch("backend.app.api.routes.printers.printer_manager", manager):
        return await async_client.post(f"/api/v1/printers/{printer_id}{url}")


@pytest.mark.asyncio
async def test_a_load_through_a_switch_not_set_up_is_refused(async_client, printer_factory, client):
    """BambuStudio's DevFilaSwitch::IsReady — every AMS on an inlet — or no load."""
    printer = await printer_factory(model="H2C")
    _fts_frame(client, ("0", _info(0xE, 1)), ("1", _info(0xE, 0xF)))

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=1&extruder_id=0")

    assert response.status_code == 409, response.text
    client._client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_a_load_through_a_switch_must_name_the_hotend(async_client, printer_factory, client):
    printer = await printer_factory(model="H2C")
    _fts_frame(client, ("0", _info(0xE, 1)))

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=1")

    assert response.status_code == 400, response.text
    client._client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_a_load_through_a_ready_switch_carries_the_hotend(async_client, printer_factory, client):
    printer = await printer_factory(model="H2C")
    _fts_frame(client, ("0", _info(0xE, 1)))

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=1&extruder_id=1")

    assert response.status_code == 200, response.text
    assert _published(client)["extruder_id"] == 1


@pytest.mark.asyncio
async def test_a_hotend_is_refused_on_a_printer_without_a_switch(async_client, printer_factory, client):
    """Without a switch each AMS is wired to one hotend and Studio never sends
    the field; an explicit one would contradict the plumbing."""
    printer = await printer_factory(model="H2D")
    client._process_message({"print": {"aux": "0"}})

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=1&extruder_id=1")

    assert response.status_code == 400, response.text
    client._client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_a_load_waits_for_the_switch_to_be_confirmed_after_a_reconnect(async_client, printer_factory, client):
    printer = await printer_factory(model="H2C")
    client.state.fts_pending_confirmation = True

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=1&extruder_id=0")

    assert response.status_code == 409, response.text
    client._client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_a_plain_load_is_unchanged_without_a_switch(async_client, printer_factory, client):
    printer = await printer_factory(model="X1C")

    response = await _post(async_client, printer.id, client, "/ams/load?tray_id=2")

    assert response.status_code == 200, response.text
    assert "extruder_id" not in _published(client)


@pytest.mark.asyncio
async def test_unloading_a_slot_no_hotend_holds_is_a_conflict(async_client, printer_factory, client):
    printer = await printer_factory(model="H2C")
    _extruders(client, {"id": 0, "snow": _snow(0, 1), "info": 2}, {"id": 1, "snow": 0xFFFF, "info": 0})

    response = await _post(async_client, printer.id, client, "/ams/unload?tray_id=7")

    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_unloading_a_held_slot_is_sent(async_client, printer_factory, client):
    printer = await printer_factory(model="H2C")
    _extruders(client, {"id": 0, "snow": _snow(0, 1), "info": 2}, {"id": 1, "snow": _snow(1, 2), "info": 2})

    response = await _post(async_client, printer.id, client, "/ams/unload?tray_id=6")

    assert response.status_code == 200, response.text
    assert _published(client)["ams_id"] == 1


# ------------------------------------------------------------- AMS preload


def test_the_ams_preload_version_is_read_from_fun2(client):
    """BambuStudio ``ams_preload_version = get_flag_bits_no_border(fun2, 21, 2)``:
    with preload the firmware parks the outgoing filament at the switch and
    pre-feeds the next one from the OTHER inlet, which is what makes a split
    across the two inlets faster (``simulate_filament_change_time``)."""
    assert client.state.ams_preload_version is None

    client._process_message({"print": {"command": "push_status", "fun2": format(1 << 21, "x")}})
    assert client.state.ams_preload_version == 1

    client._process_message({"print": {"command": "push_status", "fun2": format(3 << 21 | 1 << 19, "x")}})
    assert client.state.ams_preload_version == 3


def test_the_preload_version_travels_on_the_socket(client):
    from backend.app.services.printer_manager import printer_state_to_dict

    client._process_message({"print": {"command": "push_status", "fun2": format(1 << 21, "x")}})

    assert printer_state_to_dict(client.state, 1, "X2D")["ams_preload_version"] == 1
