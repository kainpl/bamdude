"""The chamber setpoint: the command BS sends, the target it reads, and the
two refusals it surfaces.

Three separate defects on one path, and the preheat stage depends on all three.

**The command.** BS ``DevChamber::CtrlSetChamberTemp`` publishes
``{"print": {"command": "set_ctt", "ctt_val": <int>}}``. We sent ``M141 S<n>``
over ``gcode_line``.

⚠️ Whether M141 ever worked is unverified in both directions — there is no
chamber-heated machine here, and the usual argument (BS gates M141 on
``!is_BBL_Printer()``) is about the g-code the SLICER generates for a print, not
about a live command. What is certain is which command BS sends live.

**The target.** ``DevChamber::ParseChamberV1_0`` reads two fields:
``chamber_temper`` -> current, ``ctt`` -> target. We read the first and inferred
the second from its shape, asserting 0 whenever it looked like a plain reading —
so a machine soaking at 50 °C rendered with a target of 0.

**The verdict.** BS surfaces ``errno`` on the reply: ``-2`` means refused
(low-temp filament loaded), ``-4`` means the setpoint was below 40 °C so the
printer set the target to 0 instead. We ignored both, which is exactly how a
soak that never happened looks identical to one that did: preheat waits for a
target the printer has already discarded, times out, and starts the print into a
cold chamber.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="H2D")
    c._client = MagicMock()
    c.state.connected = True
    return c


class TestTheCommand:
    def test_it_publishes_set_ctt(self) -> None:
        c = _client()
        assert c.set_chamber_temperature(50) is True

        payload = json.loads(c._client.publish.call_args[0][1])
        assert payload["print"]["command"] == "set_ctt"
        assert payload["print"]["ctt_val"] == 50
        assert "sequence_id" in payload["print"]

    def test_it_is_not_gcode(self) -> None:
        """The regression in one assertion: no ``gcode_line``, no ``M141``."""
        c = _client()
        c.set_chamber_temperature(50)

        raw = c._client.publish.call_args[0][1]
        assert "gcode_line" not in raw
        assert "M141" not in raw

    def test_a_disconnected_printer_is_refused(self) -> None:
        c = _client()
        c.state.connected = False
        assert c.set_chamber_temperature(50) is False


class TestTheTargetIsRead:
    def test_ctt_is_the_target(self) -> None:
        c = _client()
        c._update_state({"chamber_temper": 45, "ctt": 50})

        assert c.state.temperatures["chamber"] == 45.0
        assert c.state.temperatures["chamber_target"] == 50.0

    def test_a_soaking_chamber_no_longer_reports_a_target_of_zero(self) -> None:
        """The defect: a plain ``chamber_temper`` reading was taken as proof the
        heater was off, so a machine holding 50 °C drew a flat zero under a
        rising curve."""
        c = _client()
        c._update_state({"chamber_temper": 50, "ctt": 55})

        assert c.state.temperatures["chamber_target"] == 55.0

    def test_without_ctt_the_old_inference_still_applies(self) -> None:
        """Firmware that sends neither ``ctt`` nor ``device.ctc`` keeps the
        previous behaviour — no worse than before, and not a new guess."""
        c = _client()
        c._update_state({"chamber_temper": 30})

        assert c.state.temperatures["chamber_target"] == 0.0


class TestTheReplyIsRead:
    @pytest.mark.parametrize("errno", [-2, -4])
    def test_a_refused_setpoint_clears_the_optimistic_target(self, errno: int) -> None:
        """Both codes mean the chamber will not reach what we asked for. Leaving
        the local target in place is what makes preheat wait out its whole
        timeout for a number the printer already discarded."""
        c = _client()
        c.set_chamber_temperature(50)
        assert c.state.temperatures["chamber_target"] == 50.0

        c._process_message({"print": {"command": "set_ctt", "errno": errno}})

        assert c.state.temperatures["chamber_target"] == 0.0
        assert c.state.temperatures["_chamber_set_errno"] == errno

    def test_a_successful_reply_changes_nothing(self) -> None:
        c = _client()
        c.set_chamber_temperature(50)

        c._process_message({"print": {"command": "set_ctt", "errno": 0}})

        assert c.state.temperatures["chamber_target"] == 50.0
        assert "_chamber_set_errno" not in c.state.temperatures

    def test_an_unknown_errno_is_recorded_rather_than_guessed(self) -> None:
        c = _client()
        c.set_chamber_temperature(50)

        c._process_message({"print": {"command": "set_ctt", "errno": -99}})

        assert c.state.temperatures["_chamber_set_errno"] == -99
        # Not cleared: we do not know that -99 means the setpoint failed.
        assert c.state.temperatures["chamber_target"] == 50.0
