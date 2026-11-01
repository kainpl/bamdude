"""Both status shapers give a slot the same ``state``.

The WebSocket payload (``printer_state_to_dict``) and the REST status
(``_build_printer_status``) describe the same tray, and the frontend replaces
one with the other: WebSocket pushes in between, a REST fallback poll every
thirty seconds. A P1S reports an empty slot as a bare ``{"id": …}``; the
WebSocket shaper called that empty (state 9) and the REST one passed no state
at all, which the card reads as "spool loaded, slot not configured". So an
empty AMS turned amber on every poll with no message from the printer at all
(2026-11-01, P1S).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_state_to_dict


def _p1s_state() -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.raw_data = {
        "ams": [
            {
                "id": "0",
                "tray": [
                    {"id": "0"},
                    {"id": "1", "state": 10},
                    {"id": "2", "tray_type": "PLA", "state": 11},
                    {"id": "3", "exists": False},
                ],
            }
        ],
    }
    return state


EXPECTED = [9, 10, 11, None]


def test_the_websocket_payload_calls_a_bare_tray_empty():
    payload = printer_state_to_dict(_p1s_state(), printer_id=1, model="P1S")

    assert [t["state"] for t in payload["ams"][0]["tray"]] == EXPECTED


@pytest.mark.asyncio
async def test_the_rest_status_gives_every_slot_the_websocket_state(async_client, printer_factory):
    printer = await printer_factory(model="P1S")
    with patch("backend.app.api.routes.printers.printer_manager") as pm:
        pm.get_status.return_value = _p1s_state()
        pm.is_awaiting_plate_clear.return_value = False
        pm.get_drying_targets.return_value = {}
        response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

    assert response.status_code == 200, response.text
    assert [t["state"] for t in response.json()["ams"][0]["tray"]] == EXPECTED
