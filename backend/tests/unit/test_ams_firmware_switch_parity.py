"""The AMS firmware switch, against BambuStudio rather than against a guess.

The A1's AMS carries two firmware "personalities" and can reflash between them.
BamDude offered that as a dropdown with **hardcoded** labels — ``FULL`` paired
with id 0 and ``LITE`` with id 1 — while BS's own enum is::

    IDX_DC = -1, IDX_LITE = 0, IDX_AMS_AMS2_AMSHT = 1

so every click sent the opposite of what it said. It also had none of BS's three
refusals, and the state it rendered (``ams_firmware_idx_sel``) was declared on
``PrinterState`` and **never assigned by anything**, so the picker always fell
back to index 0 — the wrong one.

The fix is not better labels. BS never names these itself: it parses
``print.upgrade_state.mc_for_ams_firmware`` and builds the combo box from the
device's own ``{id, name, version}``. These tests pin that shape, because the
class of bug here is "an id whose meaning we invented".

No hardware was available; every expectation below is read from BS source
(``DeviceCore/DevFilaAmsSetting.{h,cpp}``, ``AMSSetting.cpp``, ``DevUpgrade.cpp``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.ams_capabilities import compute_ams_supports
from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState


def _client() -> BambuMQTTClient:
    """A real client — ``_update_state`` reaches into enough of ``__init__``
    that a bare ``__new__`` stub only teaches the test about its own gaps."""
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TESTSERIAL",
        access_code="12345678",
    )
    c._client = MagicMock()
    return c


def _push(**mc) -> dict:
    return {"upgrade_state": {"mc_for_ams_firmware": mc}}


class TestTheDeviceNamesItsOwnFirmwares:
    def test_the_list_is_taken_verbatim(self) -> None:
        c = _client()
        c._update_state(
            _push(
                firmware=[
                    {"id": 0, "name": "AMS Lite", "version": "00.00.06.15"},
                    {"id": 1, "name": "AMS / AMS2 / AMS HT", "version": "00.00.07.89"},
                ]
            )
        )

        assert c.state.ams_firmwares == [
            {"id": 0, "name": "AMS Lite", "version": "00.00.06.15"},
            {"id": 1, "name": "AMS / AMS2 / AMS HT", "version": "00.00.07.89"},
        ]

    def test_id_zero_is_lite_which_is_the_inversion_that_started_this(self) -> None:
        """Pinned as its own test because the old code asserted the opposite in
        a comment, and a comment cannot fail."""
        c = _client()
        c._update_state(_push(firmware=[{"id": 0, "name": "AMS Lite", "version": "1"}]))

        assert c.state.ams_firmwares[0]["id"] == 0
        assert "lite" in c.state.ams_firmwares[0]["name"].lower()

    def test_entries_are_ordered_by_id_and_deduplicated(self) -> None:
        """BS keys a ``std::map<int, …>``: sorted, last-wins on a repeat."""
        c = _client()
        c._update_state(
            _push(
                firmware=[
                    {"id": 1, "name": "second", "version": "b"},
                    {"id": 0, "name": "first", "version": "a"},
                    {"id": 1, "name": "second-again", "version": "c"},
                ]
            )
        )

        assert [fw["id"] for fw in c.state.ams_firmwares] == [0, 1]
        assert c.state.ams_firmwares[1]["name"] == "second-again"

    def test_a_malformed_entry_is_skipped_not_fatal(self) -> None:
        c = _client()
        c._update_state(_push(firmware=[{"name": "no id"}, {"id": "x"}, {"id": 2, "name": "ok", "version": ""}]))

        assert [fw["id"] for fw in c.state.ams_firmwares] == [2]


class TestTheSelectedAndRunningIds:
    def test_both_are_read(self) -> None:
        c = _client()
        c._update_state(
            _push(
                firmware=[{"id": 0, "name": "a", "version": ""}, {"id": 1, "name": "b", "version": ""}],
                current_firmware_id=1,
                current_run_firmware_id=0,
            )
        )

        assert c.state.ams_firmware_idx_sel == 1
        assert c.state.ams_firmware_idx_run == 0

    def test_an_id_outside_the_list_resets_rather_than_sticking(self) -> None:
        """BS assigns a default-constructed entry when ``m_firmwares.count(idx)``
        is zero. A kept id would point the picker at something gone."""
        c = _client()
        c._update_state(
            _push(
                firmware=[{"id": 0, "name": "a", "version": ""}],
                current_firmware_id=7,
                current_run_firmware_id=9,
            )
        )

        assert c.state.ams_firmware_idx_sel is None
        assert c.state.ams_firmware_idx_run is None

    def test_status_switching_is_carried_through(self) -> None:
        c = _client()
        c._update_state(_push(firmware=[{"id": 0, "name": "a", "version": ""}], status="SWITCHING"))

        assert c.state.ams_firmware_status == "SWITCHING"


class TestSupportIsTheDevicesAnswerNotTheModels:
    def test_an_empty_list_means_unsupported_whatever_the_model(self) -> None:
        state = PrinterState()

        assert compute_ams_supports(state, "A1")["firmware_switch"] is False

    def test_a_reported_list_means_supported_whatever_the_model(self) -> None:
        """The old gate was ``model in {"A1"}``. A model list cannot know that an
        AMS was swapped or that firmware predates the feature."""
        state = PrinterState()
        state.ams_firmwares = [{"id": 0, "name": "AMS Lite", "version": ""}]

        assert compute_ams_supports(state, "P1S")["firmware_switch"] is True


class TestThePublishAndItsHold:
    def test_it_sends_the_device_id_under_upgrade(self) -> None:
        c = _client()
        c.state.connected = True

        ok, seq = c.ams_firmware_switch(1)

        assert ok is True
        payload = c._client.publish.call_args[0][1]
        assert '"mc_for_ams_firmware_upgrade"' in payload
        assert '"id": 1' in payload
        assert '"src_id": 1' in payload
        # Not under "print" — this one command lives in its own namespace.
        assert '"upgrade"' in payload

    def test_it_latches_switching_locally_like_bs_does(self) -> None:
        c = _client()
        c.state.connected = True

        c.ams_firmware_switch(1)

        assert c.state.ams_firmware_status == "SWITCHING"
        assert c.state.ams_firmware_idx_sel == 1

    def test_a_report_already_in_flight_cannot_undo_the_choice(self) -> None:
        """The hold exists for exactly one thing: the push that was sent before
        our command landed still carries the OLD selection."""
        c = _client()
        c.state.connected = True
        c.ams_firmware_switch(1)

        c._update_state(
            _push(
                firmware=[{"id": 0, "name": "a", "version": ""}, {"id": 1, "name": "b", "version": ""}],
                current_firmware_id=0,
            )
        )

        assert c.state.ams_firmware_idx_sel == 1

    def test_the_hold_expires_so_a_real_refusal_is_visible(self) -> None:
        c = _client()
        c.state.connected = True
        c.ams_firmware_switch(1)
        c.state.ams_settings_hold["ams_firmware_switch"] = 0.0  # epoch, i.e. long expired

        c._update_state(
            _push(
                firmware=[{"id": 0, "name": "a", "version": ""}, {"id": 1, "name": "b", "version": ""}],
                current_firmware_id=0,
            )
        )

        assert c.state.ams_firmware_idx_sel == 0


class TestTheSignalsTheRefusalsAreBuiltOn:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("DOWNLOADING", True),
            ("FLASHING", True),
            ("UPGRADE_REQUEST", True),
            ("PRE_FLASH_START", True),
            ("PRE_FLASH_SUCCESS", True),
            ("IDLE", False),
            ("UPGRADE_SUCCESS", False),
        ],
    )
    def test_printer_upgrade_status_is_parsed(self, status: str, expected: bool) -> None:
        """The five in-progress values are BS's ``is_in_upgrading()``
        (DevUpgrade.cpp); everything else is not a flash."""
        from backend.app.api.routes.ams_settings import _UPGRADING_STATUSES

        c = _client()
        c._update_state({"upgrade_state": {"status": status}})

        assert c.state.firmware_upgrade_status == status
        assert (status in _UPGRADING_STATUSES) is expected

    def test_filament_presence_comes_from_extruder_info_bit_1(self) -> None:
        """BS: ``m_ext_has_filament = get_flag_bits(info, 1)``."""
        c = _client()
        c._update_state({"device": {"extruder": {"info": [{"id": 0, "info": 0b10}, {"id": 1, "info": 0b01}]}}})

        assert c.state.ext_has_filament == {0: True, 1: False}

    def test_extruder_entries_without_an_id_fall_back_to_position(self) -> None:
        c = _client()
        c._update_state({"device": {"extruder": {"info": [{"info": 0b10}]}}})

        assert c.state.ext_has_filament == {0: True}
