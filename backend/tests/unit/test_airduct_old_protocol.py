"""The old protocol had no fan controls at all, and the code to drive it was
already written.

Registry item F2. BS makes both protocols look alike before anything reads them:
``DevFan::converse_to_duct`` builds parts 1/2/3 by hand and sets the mode to
``-1`` whenever the printer reports no airduct modes, so one widget drives a P1S
and an X2D alike.

We never did that step. On a P1S ``airduct_parts`` stays empty, every consumer
reads it, and the card offers **no fan control whatsoever** — while the ``M106``
branch inside ``set_fan_speed`` sat unreachable, because part ids only ever came
from the very list it was meant to bypass. A whole protocol's worth of control
was written and could not be arrived at.

⚠️ **The first version of this gated the aux and chamber fans on whether the
printer had reported a speed for them — and hardware disproved it.** An A1 Mini
has part cooling only, ignores g-code aimed at anything else, and still echoes
the part-cooling percentage into ``big_fan1_speed`` and ``big_fan2_speed``:
setting one fan to 100 % lit three badges at 100 %. Those fields are published
regardless of the hardware, so they cannot answer a hardware question.
``support_aux_fan`` / ``support_chamber_fan`` can, and BS reads exactly them.

⚠️ We do read them as **two** flags where BS reads one. BS parses both and then
``GetSupportChamberFan()`` returns ``is_support_aux_fan`` — the chamber field it
just parsed goes unused there. That is a slip in the getter, not a data model.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import (
    FAN_PART_ID_AUX,
    FAN_PART_ID_CHAMBER,
    FAN_PART_ID_COOLING,
    BambuMQTTClient,
    PrinterState,
    airduct_parts_effective,
)


def _old(cooling=None, aux=None, chamber=None, *, has_aux=True, has_chamber=True) -> PrinterState:
    """A printer that reports flat fan fields and no airduct at all.

    ``has_aux`` / ``has_chamber`` stand in for the printer's own
    ``support_*_fan`` bools — the only thing that says which fans exist.
    """
    s = PrinterState()
    s.cooling_fan_speed = cooling
    s.big_fan1_speed = aux
    s.big_fan2_speed = chamber
    s.print_option_support = {"aux_fan": has_aux, "chamber_fan": has_chamber}
    return s


class TestTheSynthesis:
    def test_part_cooling_is_always_there(self) -> None:
        """BS adds it unconditionally — every FDM printer has one."""
        assert FAN_PART_ID_COOLING in airduct_parts_effective(_old())

    def test_the_ids_are_bs_own(self) -> None:
        """``AIR_FUN``: cooling 1, remote cooling 2, chamber 3. A different id
        would be a fan the printer does not have."""
        assert (FAN_PART_ID_COOLING, FAN_PART_ID_AUX, FAN_PART_ID_CHAMBER) == (1, 2, 3)

    def test_a_fan_the_machine_does_not_have_is_not_invented(self) -> None:
        assert FAN_PART_ID_AUX not in airduct_parts_effective(_old(cooling=40, has_aux=False))

    def test_a_fitted_aux_fan_appears(self) -> None:
        assert FAN_PART_ID_AUX in airduct_parts_effective(_old(aux=0))

    def test_a_reported_speed_is_not_evidence_of_a_fan(self) -> None:
        """The A1 Mini in one assertion. It has part cooling only, and echoes
        that percentage into every flat field — so before this, driving one fan
        to 100 % lit three badges at 100 %."""
        parts = airduct_parts_effective(_old(cooling=100, aux=100, chamber=100, has_aux=False, has_chamber=False))

        assert set(parts) == {FAN_PART_ID_COOLING}

    def test_the_chamber_fan_does_not_depend_on_the_aux_one(self) -> None:
        """⚠️ Two flags, where BS's getter uses one for both."""
        parts = airduct_parts_effective(_old(chamber=55, has_aux=False))

        assert FAN_PART_ID_CHAMBER in parts
        assert FAN_PART_ID_AUX not in parts

    def test_live_speeds_are_carried_through(self) -> None:
        """BS synthesises with ``state = 0``; ours must not, because this dict is
        what the badge reads. Zeroing it would blank a running fan."""
        parts = airduct_parts_effective(_old(cooling=100, aux=60, chamber=30))

        assert parts[FAN_PART_ID_COOLING]["state"] == 100
        assert parts[FAN_PART_ID_AUX]["state"] == 60
        assert parts[FAN_PART_ID_CHAMBER]["state"] == 30

    def test_synthesised_parts_are_fans_not_air_doors(self) -> None:
        """Type 1 is an air door, and the fan list filters on this."""
        assert airduct_parts_effective(_old())[FAN_PART_ID_COOLING]["type"] == 0


class TestARealAirductWins:
    def test_nothing_is_synthesised_when_the_printer_reports_parts(self) -> None:
        s = _old(cooling=50, aux=50, chamber=50)
        s.airduct_parts = {10: {"type": 0, "state": 70, "range_start": 0, "range_end": 100}}

        assert airduct_parts_effective(s) == s.airduct_parts

    def test_the_flat_fields_do_not_leak_in_beside_it(self) -> None:
        """Otherwise a P2S would show its aux fan twice — once from the airduct
        under its proper name, once from a flat field."""
        s = _old(cooling=50, aux=50)
        s.airduct_parts = {2: {"type": 0, "state": 70, "range_start": 0, "range_end": 100}}

        assert set(airduct_parts_effective(s)) == {2}


class TestTheStateIsNotPolluted:
    def test_synthesis_does_not_write_into_the_reported_inventory(self) -> None:
        """``airduct_parts`` answers "what did the printer send". The wire
        protocol is chosen from exactly that question, so an invented entry
        there would publish ``set_fan`` to a machine that only speaks M106."""
        s = _old(cooling=40, aux=40)
        airduct_parts_effective(s)

        assert s.airduct_parts == {}


class TestTheM106BranchIsFinallyReachable:
    @pytest.fixture
    def client(self) -> BambuMQTTClient:
        c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="P1S123", access_code="12345678", model="P1S")
        c._client = MagicMock()
        c.state.connected = True
        c.state.big_fan1_speed = 0
        return c

    def test_an_old_protocol_fan_can_be_driven(self, client: BambuMQTTClient) -> None:
        assert client.set_fan_speed(FAN_PART_ID_AUX, 50) is True

    def test_it_goes_out_as_m106_on_the_0_255_scale(self, client: BambuMQTTClient) -> None:
        """The two protocols disagree about the scale; 50 % is 128, not 50."""
        client.set_fan_speed(FAN_PART_ID_AUX, 50)
        payload = json.loads(client._client.publish.call_args[0][1])

        assert payload["print"]["command"] == "gcode_line"
        assert payload["print"]["param"].strip() == "M106 P2 S128"

    def test_a_synthesised_inventory_does_not_flip_it_to_set_fan(self, client: BambuMQTTClient) -> None:
        """The protocol choice reads the raw reported parts, never the
        synthesised ones — that separation is the whole reason they are two
        different things."""
        client.state.enable_np = True
        client.set_fan_speed(FAN_PART_ID_AUX, 50)
        payload = json.loads(client._client.publish.call_args[0][1])

        assert payload["print"]["command"] == "gcode_line"


class TestTheModelAnswersWhenThePrinterHasNot:
    """The flags also live in the mirrored BambuStudio config, so the answer is
    right from the first frame rather than after the printer gets round to
    mentioning them."""

    def _bare(self, firmware: str | None) -> PrinterState:
        s = PrinterState()
        s.firmware_version = firmware
        s.cooling_fan_speed = 100
        s.big_fan1_speed = 100
        s.big_fan2_speed = 100
        return s

    @pytest.mark.parametrize("firmware", ["01.08.00.00", None])
    def test_an_a1_mini_gets_part_cooling_only(self, firmware: str | None) -> None:
        """Reported on hardware: one controllable fan, no chamber fan at all,
        and the same percentage echoed into all three fields. Answered with or
        without a known firmware version — a fan that does not exist must not
        appear during the window before ``get_version``."""
        assert set(airduct_parts_effective(self._bare(firmware), "A1 Mini")) == {FAN_PART_ID_COOLING}

    @pytest.mark.parametrize("model", ["A1", "A2L"])
    def test_the_rest_of_the_a_series_agrees(self, model: str) -> None:
        assert set(airduct_parts_effective(self._bare("01.08.00.00"), model)) == {FAN_PART_ID_COOLING}

    @pytest.mark.parametrize("model", ["P1S", "X1C", "P1P"])
    def test_machines_that_do_have_them_keep_all_three(self, model: str) -> None:
        parts = airduct_parts_effective(self._bare("01.08.00.00"), model)

        assert set(parts) == {FAN_PART_ID_COOLING, FAN_PART_ID_AUX, FAN_PART_ID_CHAMBER}

    def test_an_unknown_model_offers_only_what_is_certain(self) -> None:
        """No config, no live flag — every printer has part cooling, and
        nothing else can be assumed."""
        assert set(airduct_parts_effective(self._bare("01.08.00.00"), "NotABambu")) == {FAN_PART_ID_COOLING}

    def test_the_live_flag_beats_the_config(self) -> None:
        """An aux fan is a kit on some machines; the printer knows, the model
        name cannot."""
        s = self._bare("01.08.00.00")
        s.print_option_support = {"aux_fan": True, "chamber_fan": False}

        assert set(airduct_parts_effective(s, "A1 Mini")) == {FAN_PART_ID_COOLING, FAN_PART_ID_AUX}
