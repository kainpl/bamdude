"""What scale a fan number is in comes from the field it arrived in, never from
how big it is.

Registry F7 + F8, which turned out to be one piece of work rather than two.

BS ``DevFan::ParseV1_0`` has two branches and prefers the packed one:

* ``fan_gear`` — one 32-bit word carrying three bytes: part cooling, auxiliary,
  chamber. Each is already 0-255;
* otherwise the three named fields, each a raw 0-15 that BS maps with
  ``round(floor(v / 1.5) * 25.5)`` — eleven distinct steps, not sixteen.

We read only the named fields, and guessed their scale by magnitude::

    if speed <= 15:   treat as a gear
    elif speed <= 255: treat as already-a-percentage

⚠️ A magnitude cannot answer that question. A genuine 10 out of 255 — four
percent — was read as gear 10 and displayed as 67 %. And ``fan_gear`` was not
read at all, so a printer that sends it would have shown nothing.

⚠️ The linear ``v * 100 / 15`` also disagreed with BS on **ten of the sixteen**
raw values, and one of those matters on its own: raw ``1`` is **0 %** in BS —
the fan is off — and we showed 7 %, i.e. running.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import (
    BambuMQTTClient,
    _fan_gear_bytes,
    _percent_from_byte,
)


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="P1S001", access_code="12345678", model="P1S")
    c._client = MagicMock()
    c.state.connected = True
    return c


def _bs_percent(raw: int) -> int:
    """BS's own arithmetic, spelled out: raw -> 0-255 -> percent."""
    return round(round(math.floor(raw / 1.5) * 25.5) * 100 / 255)


class TestTheNamedFieldsAreGears:
    @pytest.mark.parametrize("raw", range(16))
    def test_every_raw_value_matches_bs(self, raw: int) -> None:
        assert _client()._percent_from_gear(raw) == _bs_percent(raw)

    def test_raw_one_is_off_not_seven_percent(self) -> None:
        """The disagreement that changes what an operator sees: BS floors this
        to gear 0. We used to show a stopped fan as running."""
        assert _client()._percent_from_gear(1) == 0

    def test_the_steps_are_the_eleven_gears(self) -> None:
        """Sixteen raw values collapse onto eleven percentages — some raws share
        a gear. A linear map invents five values the printer never means."""
        seen = {_client()._percent_from_gear(v) for v in range(16)}

        assert seen == {0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100}

    def test_full_speed_is_full_speed(self) -> None:
        assert _client()._percent_from_gear(15) == 100

    @pytest.mark.parametrize("junk", [None, "", "abc", {}])
    def test_unreadable_stays_unknown(self, junk) -> None:
        """``None`` means "not reported", which is a different answer from 0 and
        is what the fan-inventory gate reads."""
        assert _client()._percent_from_gear(junk) is None


class TestAValueTooBigIsNotSilentlyRescaled:
    def test_it_is_clamped_rather_than_read_as_a_percentage(self) -> None:
        """⚠️ The old ``elif speed <= 255`` branch is what made 10 ambiguous. BS
        has no such branch; if hardware really sends a wider range we would
        rather learn it from the log than from a wrong reading."""
        assert _client()._percent_from_gear(200) == 100

    def test_it_says_so_once(self, caplog: pytest.LogCaptureFixture) -> None:
        c = _client()
        with caplog.at_level("WARNING"):
            c._percent_from_gear(200)
            c._percent_from_gear(201)

        assert sum("out of the 0-15 range" in r.message for r in caplog.records) == 1


class TestThePackedWord:
    def test_the_bytes_are_bs_own_order(self) -> None:
        """``ParseV1_0``: byte 0 cooling, byte 1 aux, byte 2 chamber."""
        assert _fan_gear_bytes(0x0A1E32) == (0x32, 0x1E, 0x0A)

    def test_the_bytes_are_already_0_255(self) -> None:
        assert _percent_from_byte(255) == 100
        assert _percent_from_byte(0) == 0
        assert _percent_from_byte(128) == 50

    @pytest.mark.parametrize("junk", [None, "abc", {}])
    def test_an_unreadable_word_is_ignored(self, junk) -> None:
        assert _fan_gear_bytes(junk) is None


class TestWhichBranchWins:
    def test_the_packed_word_is_preferred(self) -> None:
        """BS checks ``fan_gear`` first. A printer sending both must not have its
        packed value overwritten by the named ones."""
        c = _client()
        c._update_state({"fan_gear": 0x0000FF00, "cooling_fan_speed": "15", "big_fan1_speed": "0"})

        assert c.state.big_fan1_speed == 100
        assert c.state.cooling_fan_speed == 0

    def test_without_it_the_named_fields_are_used(self) -> None:
        c = _client()
        c._update_state({"cooling_fan_speed": "15", "big_fan1_speed": "2"})

        assert c.state.cooling_fan_speed == 100
        assert c.state.big_fan1_speed == 10

    def test_a_field_that_did_not_arrive_is_left_alone(self) -> None:
        """Diff pushes omit most keys; clearing on absence would flap."""
        c = _client()
        c._update_state({"cooling_fan_speed": "15", "big_fan1_speed": "9"})
        c._update_state({"cooling_fan_speed": "0"})

        assert c.state.big_fan1_speed == 60
