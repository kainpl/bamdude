"""The door-open check is gated on the printer's own bit, not on a model list.

BS (``PrintOptionsDialog::UpdateOptionOpenDoorCheck``)::

    is_support_door_open_check = get_flag_bits(fun, 12);   // live
    if (!obj->support_door_open_check())            -> hide
    if (support_safety_options(printer_type))       -> hide  (moved to Safety tab)

We decoded ``fun`` bit 12 in **two** places, read it in none, and gated the row
on ``has_door_sensor`` instead — a list answering "is there a door sensor",
which is a different question from "does this firmware offer the door-open
check". It excluded the whole H2 family, so those machines could not switch the
protection on at all.

Parsing a bit and discarding it is worse than not parsing it: the work looks
done.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import BambuMQTTClient, PrinterState
from backend.app.services.printer_capabilities import compute_printer_supports


def _state(**support) -> PrinterState:
    s = PrinterState()
    s.print_option_support = dict(support)
    return s


class TestTheLiveBitDecides:
    def test_the_h2_family_gains_the_row_when_it_reports_the_bit(self) -> None:
        """The models the old list left out entirely."""
        for model in ("H2D", "H2D Pro", "H2C", "H2S"):
            assert compute_printer_supports(_state(open_door_check=True), model, {})["open_door_check"] is True, model

    def test_a_printer_reporting_zero_does_not_get_the_row(self) -> None:
        """Assign, not "the model list says yes anyway"."""
        assert compute_printer_supports(_state(open_door_check=False), "X1C", {})["open_door_check"] is False

    def test_the_model_list_is_still_the_fallback_before_the_first_push(self) -> None:
        """Absent from the dict means "not reported", and an X1C has the sensor —
        so the pre-push window keeps today's answer instead of hiding a row the
        machine does have."""
        assert compute_printer_supports(_state(), "X1C", {})["open_door_check"] is True
        assert compute_printer_supports(_state(), "P1P", {})["open_door_check"] is False


class TestTheSafetyTabStillWins:
    def test_a_safety_tab_model_hides_the_row_even_reporting_support(self) -> None:
        """BS's mutual exclusion: on X2D/P2S the control lives in Safety, and
        showing it in both places would be two switches for one setting."""
        for model in ("X2D", "P2S"):
            assert compute_printer_supports(_state(open_door_check=True), model, {})["open_door_check"] is False, model


class TestTheBitReachesTheDictAtAll:
    def test_fun_bit_12_lands_where_the_capability_computer_looks(self) -> None:
        """The actual defect: it was decoded into ``print_options`` — a
        different container from the one ``compute_printer_supports`` reads —
        so no amount of gating on it would have worked."""
        c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="H2D")
        c._client = MagicMock()

        c._process_message({"print": {"command": "push_status", "fun": hex(1 << 12)}})

        assert c.state.print_option_support.get("open_door_check") is True
        assert compute_printer_supports(c.state, "H2D", {})["open_door_check"] is True

    def test_a_printer_that_sends_no_fun_leaves_the_key_absent(self) -> None:
        """ "Not reported" has to stay distinguishable from "reported false",
        which a plain bool default cannot express."""
        c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="X1C")
        c._client = MagicMock()

        c._process_message({"print": {"command": "push_status", "gcode_state": "IDLE"}})

        assert "open_door_check" not in c.state.print_option_support


class TestTheFirstLayerFallbackComesFromTheConfig:
    """``has_ai`` answered "does it have a camera", which is not the same
    question. Measured across all fifteen configs: the H2 family says
    ``support_first_layer_inspect: false`` while ``has_ai`` said yes, so four
    models were offered a row for a feature the machine does not have."""

    def test_the_h2_family_no_longer_claims_first_layer_inspection(self) -> None:
        for model in ("H2C", "H2D", "H2D Pro", "H2S"):
            assert compute_printer_supports(_state(), model, {})["first_layer_inspector"] is False, model

    def test_the_x1_family_still_has_it(self) -> None:
        for model in ("X1C", "X1E"):
            assert compute_printer_supports(_state(), model, {})["first_layer_inspector"] is True, model

    def test_the_live_bit_still_wins_over_the_config(self) -> None:
        """This only decides the pre-push window; a printer that says it has the
        feature is believed."""
        assert compute_printer_supports(_state(first_layer_inspector=True), "H2D", {})["first_layer_inspector"] is True
