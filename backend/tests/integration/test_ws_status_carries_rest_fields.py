"""The WebSocket status shaper carries what the REST shaper does.

A page loads its printer status over REST and then replaces it with every
WebSocket push, so a field only the REST shaper builds is shown after a reload
and lost at the next push. That is how an H2C rack slot lost its material label
(``filament_type``) and how an old-protocol printer's airduct mode flipped from
-1 to 0 in the browser (upstream 808e461c5).

Both shapers are run over the SAME state and compared.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes import printers
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_state_to_dict

# Fields the REST answer carries and a push deliberately does not — each with
# its reason. A NEW REST field must be added to the WebSocket shaper or here.
REST_ONLY = {
    # Identity of the row the status was asked for; a push is keyed by printer id.
    "id",
    "name",
    "connector_identity",
    # Kept REST-only on purpose (upstream cacaaf4da, adapted): the stage names
    # have a polling query of their own, and nothing reads the other two off a
    # status. A push that carried them would pay 300-450 bytes for no reader.
    "ams_exists",
    "print_options",
    "stg_names",
}


def _rack_state() -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.nozzle_rack = [
        {
            "id": 16,
            "type": "HS01",
            "diameter": "0.4",
            "wear": 3,
            "stat": 1,
            "max_temp": 300,
            "serial_number": "SN1",
            "filament_color": "FF0000FF",
            "filament_id": "GFA00",
            "filament_type": "PLA",
        }
    ]
    return state


async def _both(printer_factory, db_session, monkeypatch, state: PrinterState, model: str = "H2C"):
    printer = await printer_factory(name="H2C", model=model)
    monkeypatch.setattr(printers.printer_manager, "get_status", lambda _id: state)
    rest = (await printers._build_printer_status(printer, db_session)).model_dump()
    ws = printer_state_to_dict(state, printer.id, printer.model)
    return rest, ws


@pytest.mark.asyncio
@pytest.mark.integration
class TestBothShapersAgree:
    async def test_each_rack_slot_is_the_same_in_both(self, printer_factory, db_session, monkeypatch):
        rest, ws = await _both(printer_factory, db_session, monkeypatch, _rack_state())

        assert ws["nozzle_rack"][0]["filament_type"] == "PLA"
        assert ws["nozzle_rack"] == rest["nozzle_rack"]

    async def test_a_printer_without_airduct_mode_lists_reports_the_same_mode(
        self, printer_factory, db_session, monkeypatch
    ):
        """The old protocol: no mode lists, and the effective mode is not the raw default 0."""
        state = _rack_state()
        assert not state.airduct_modes

        rest, ws = await _both(printer_factory, db_session, monkeypatch, state)

        assert ws["airduct_mode"] == rest["airduct_mode"]
        assert ws["airduct_sub_mode"] == rest["airduct_sub_mode"]

    async def test_every_rest_field_is_pushed_or_listed_as_rest_only(self, printer_factory, db_session, monkeypatch):
        rest, ws = await _both(printer_factory, db_session, monkeypatch, _rack_state())

        assert sorted(set(rest) - set(ws) - REST_ONLY) == []
