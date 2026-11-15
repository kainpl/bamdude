"""The REST status and the WebSocket push describe one printer the same way, all the way down.

The page loads a status over REST and merges every push over it, but a LIST is
replaced whole — so a field only one shaper builds flickers: the REST answer
had no ``dry_status`` (a unit that cannot dry offered Start), and the push had
no ``cols`` (a multi-colour spool was drawn in its first colour). Upstream
113f08a1a / cacaaf4da, adapted.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes import printers
from backend.app.services.bambu_mqtt import NozzleInfo, PrinterState
from backend.app.services.printer_manager import printer_state_to_dict


def _ams_state() -> PrinterState:
    """A regular AMS with a multi-colour tray and a refused dryer, an AMS-HT, two external spools."""
    state = PrinterState()
    state.connected = True
    state.raw_data = {
        "ams": [
            {
                "id": "0",
                "temp": "24.5",
                "humidity_raw": "31",
                "sn": "AMS0SN",
                "sw_ver": "01.02.03",
                "module_type": "n3f",
                "dry_time": 0,
                "dry_status": 5,
                "dry_sub_status": 2,
                "dry_sf_reason": [1, 8],
                "tray": [
                    {
                        "id": "0",
                        "tray_type": "PLA",
                        "tray_color": "FF0000FF",
                        "cols": ["FF0000FF", "00FF00FF"],
                        "ctype": 0,
                        "remain": 62,
                        "remain_g": 620,
                        "tray_sub_brands": "PLA Basic",
                        "tray_info_idx": "GFA00",
                        "tag_uid": "0000000000000000",
                        "tray_uuid": "A" * 32,
                        "cali_idx": 3,
                        "k": 0.02,
                        "nozzle_temp_min": 190,
                        "nozzle_temp_max": 240,
                        "drying_temp": 55,
                        "drying_time": 8,
                        "state": 11,
                        "exists": True,
                    },
                    {"id": "1", "tray_type": "PETG", "tray_color": "0000FFFF", "remain": -1, "state": 10},
                    {"id": "2"},
                    {"id": "3", "tray_type": "", "state": 3, "exists": False},
                ],
            },
            {
                "id": "128",
                "temp": "40",
                "dry_time": 95,
                "dry_status": 2,
                "dry_sub_status": 1,
                "tray": [{"id": "0", "tray_type": "PA-CF", "tray_color": "101010FF", "remain": 80, "state": 10}],
            },
        ],
        "vt_tray": [
            {
                "id": "254",
                "tray_type": "TPU",
                "tray_color": "FFFFFFFF",
                "cols": ["FFFFFFFF", "000000FF"],
                "remain": 0,
                "remain_g": 40,
                "tag_uid": "",
                "cali_idx": -1,
            },
            {"id": "255", "tray_type": "ABS", "tray_color": "00FF00FF"},
        ],
    }
    return state


async def _both(printer_factory, db_session, monkeypatch, state: PrinterState, model: str = "X2D"):
    printer = await printer_factory(name="Both", model=model)
    monkeypatch.setattr(printers.printer_manager, "get_status", lambda _id: state)
    rest = (await printers._build_printer_status(printer, db_session)).model_dump()
    ws = printer_state_to_dict(state, printer.id, printer.model)
    return rest, ws


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheAmsIsTheSameInBoth:
    async def test_every_unit_and_tray_key_is_the_same(self, printer_factory, db_session, monkeypatch):
        rest, ws = await _both(printer_factory, db_session, monkeypatch, _ams_state())

        assert len(rest["ams"]) == 2 and len(rest["ams"][0]["tray"]) == 4
        assert ws["ams"] == rest["ams"]

    async def test_the_external_spools_are_the_same(self, printer_factory, db_session, monkeypatch):
        rest, ws = await _both(printer_factory, db_session, monkeypatch, _ams_state())

        assert len(rest["vt_tray"]) == 2
        assert ws["vt_tray"] == rest["vt_tray"]

    async def test_the_rest_answer_says_why_a_unit_cannot_dry(self, printer_factory, db_session, monkeypatch):
        """It defaulted to "off, no reason", and the card offered Start on a unit that cannot dry."""
        rest, _ws = await _both(printer_factory, db_session, monkeypatch, _ams_state())

        unit = rest["ams"][0]
        assert (unit["dry_status"], unit["dry_sub_status"], unit["dry_sf_reason"]) == (5, 2, [1, 8])

    async def test_a_push_keeps_every_colour_of_a_multi_colour_spool(self, printer_factory, db_session, monkeypatch):
        _rest, ws = await _both(printer_factory, db_session, monkeypatch, _ams_state())

        tray = ws["ams"][0]["tray"][0]
        assert (tray["cols"], tray["ctype"], tray["remain_g"]) == (["ff0000ff", "00ff00ff"], 0, 620)
        assert ws["vt_tray"][0]["cols"] == ["ffffffff", "000000ff"]
        assert ws["vt_tray"][0]["remain_g"] == 40

    async def test_every_shared_top_level_field_agrees(self, printer_factory, db_session, monkeypatch):
        state = _ams_state()
        state.ams_extruder_map = {"0": 1, "128": 0}
        state.raw_data["ams_extruder_map"] = {"0": 1, "128": 0}

        rest, ws = await _both(printer_factory, db_session, monkeypatch, state)

        differing = sorted(key for key in set(rest) & set(ws) if rest[key] != ws[key])
        assert differing == []

    async def test_the_extruder_map_survives_a_replaced_raw_frame(self, printer_factory, db_session, monkeypatch):
        """The REST shaper reads the state attribute for this reason; the push read ``raw_data``."""
        state = _ams_state()
        state.ams_extruder_map = {"0": 1}

        rest, ws = await _both(printer_factory, db_session, monkeypatch, state)

        assert ws["ams_extruder_map"] == rest["ams_extruder_map"] == {"0": 1}


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheSevenPushedFields:
    async def test_nozzles_developer_mode_macro_and_airduct_are_pushed(self, printer_factory, db_session, monkeypatch):
        state = _ams_state()
        state.nozzles = [
            NozzleInfo(nozzle_type="hardened_steel", nozzle_diameter="0.6"),
            NozzleInfo(nozzle_type="stainless_steel", nozzle_diameter="0.4"),
        ]
        state.developer_mode = True
        state.macro_executing = "swap_mode_change_table"
        state.airduct_modes = {0: {}, 1: {}}
        state.print_option_support = {"cooling_filter": True}

        rest, ws = await _both(printer_factory, db_session, monkeypatch, state)

        for key in ("nozzles", "developer_mode", "macro_executing", "airduct_modes", "supports_cooling_filter"):
            assert ws[key] == rest[key], key
        assert ws["nozzles"][0]["nozzle_diameter"] == "0.6"
        assert ws["macro_executing"] == "swap_mode_change_table"
        assert ws["airduct_modes"] == [0, 1]
        assert ws["supports_cooling_filter"] is True

    async def test_the_running_archive_is_pushed_from_the_bound_run(self, printer_factory):
        from backend.app.services.print_run_binding import bind_print_run, discard_print_run
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory(name="P", model="P1S")
        state = _ams_state()
        state.state = "RUNNING"
        bind_print_run(
            printer_manager,
            printer_id=printer.id,
            archive_id=4711,
            observed_subtask_id=None,
            archive_identity="identity-4711",
            client_generation=None,
            origin="dispatch",
        )
        try:
            ws = printer_state_to_dict(state, printer.id, printer.model)
        finally:
            discard_print_run(printer_manager, printer.id, 4711)

        assert ws["current_archive_id"] == 4711

    async def test_a_running_print_with_no_bound_run_leaves_the_loaded_archive_alone(self, printer_factory):
        """The page merges a push over the REST answer: an absent key keeps what REST read from the database."""
        printer = await printer_factory(name="P", model="P1S")
        state = _ams_state()
        state.state = "RUNNING"

        ws = printer_state_to_dict(state, printer.id, printer.model)

        assert "current_archive_id" not in ws

    async def test_an_idle_printer_pushes_no_archive(self, printer_factory):
        printer = await printer_factory(name="P", model="P1S")
        state = _ams_state()
        state.state = "IDLE"

        assert printer_state_to_dict(state, printer.id, printer.model)["current_archive_id"] is None


class TestABroadcastIsSentWhenOnlyAPushedFieldMoves:
    def _key(self, state):
        from backend.app.main import _printer_status_effect_key

        return _printer_status_effect_key(state)

    def test_a_nozzle_swap_on_an_idle_printer(self):
        state = _ams_state()
        before = self._key(state)
        state.nozzles = [NozzleInfo(nozzle_type="hardened_steel", nozzle_diameter="0.6")]

        assert self._key(state) != before

    def test_a_macro_starting(self):
        state = _ams_state()
        before = self._key(state)
        state.macro_executing = "swap_mode_start"

        assert self._key(state) != before

    def test_developer_mode(self):
        state = _ams_state()
        before = self._key(state)
        state.developer_mode = True

        assert self._key(state) != before


class TestThePushedFieldsReachTheBrowser:
    """The live projection passes a finite field list; a pushed field outside it never arrives."""

    def _shape(self, data, *permissions, row_owner=7):
        from backend.app.services import live_projection
        from backend.app.services.live_authority import Entitlements

        prepared = live_projection.PreparedEvent({"type": "printer_status", "printer_id": 1, "data": data})
        prepared.run_archive, prepared.run_identity = 42, "row"
        prepared.rows = {
            ("archive", 42): {"id": 42, "created_by_id": row_owner, "printer_id": 1, "recovery_identity": "row"}
        }
        return live_projection.shape(prepared, Entitlements(frozenset(permissions), None, 7, "account"))["data"]

    def test_the_card_fields_pass(self):
        data = {
            "nozzles": [{"nozzle_type": "hardened_steel", "nozzle_flow": "", "nozzle_diameter": "0.6"}],
            "developer_mode": True,
            "macro_executing": "swap_mode_start",
            "airduct_modes": [0, 1],
            "supports_cooling_filter": True,
            "ams_mapping": [1, -1, 5],
        }

        shaped = self._shape(data, "printers:read")

        assert {key: shaped[key] for key in data} == data

    def test_an_absent_mapping_stays_absent(self):
        assert "ams_mapping" not in self._shape({"progress": 5}, "printers:read")

    def test_the_running_archive_is_for_a_viewer_who_may_see_that_print(self):
        data = {"current_archive_id": 42}

        assert self._shape(data, "printers:read", "archives:read_own")["current_archive_id"] == 42
        assert "current_archive_id" not in self._shape(data, "printers:read", "archives:read_own", row_owner=99)
        assert self._shape({"current_archive_id": None}, "printers:read")["current_archive_id"] is None

    def test_an_archive_id_of_another_run_than_the_one_judged_is_not_sent(self):
        """The producer reads the bound run a second time; the viewer's right was judged for the
        run the event was prepared with (42). An id read after the run changed is not that run's."""
        shaped = self._shape({"current_archive_id": 43}, "printers:read", "archives:read_own")

        assert "current_archive_id" not in shaped
