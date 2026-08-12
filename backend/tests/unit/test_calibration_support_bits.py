"""PA / flow calibration support: the right fields, and Bambu's own clamps.

This was read from a top-level ``func`` int at bits 15/16, citing BS. There is
no such field — BS's only ``"func"`` is ``part.func`` *inside* an airduct part —
and the two bit positions belong to two different fields it does read:

    home_flag  bit 15 -> flow, bit 16 -> pa   (legacy, parse_home_flag)
    fun        bit  6 -> flow, bit  7 -> pa   (new protocol, hex string)

It also OR'd rather than assigned, so a capability seen once could never be
withdrawn.

And it had neither clamp. BS overrides both bits by printer series, with its own
comment saying why::

    if (is_series_o()) is_support_flow_calibration = false;
        // todo: Temp modification due to incorrect machine push message for H2D
    if (is_series_p()) is_support_pa_calibration = false;
        // todo: Temp modification due to incorrect machine push message for P

That is firmware Bambu knows is lying: the machine advertises a capability it
does not have. A data-driven port misses this by construction — the config is
right, the *printer* is wrong — which is exactly why the clamps are pinned here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, parse_hex_bitfield

FLOW_BIT_FUN = 6
PA_BIT_FUN = 7
FLOW_BIT_HOME = 15
PA_BIT_HOME = 16


def _client(model: str | None = "X1C") -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model=model)
    c._client = MagicMock()
    return c


class TestTheFieldsAreTheOnesBSReads:
    def test_fun_bits_6_and_7(self) -> None:
        c = _client("X1C")
        c._update_state({"fun": hex((1 << FLOW_BIT_FUN) | (1 << PA_BIT_FUN))})

        assert c.state.is_support_auto_flow_calibration is True
        assert c.state.is_support_pa_calibration is True

    def test_home_flag_bits_15_and_16(self) -> None:
        c = _client("X1C")
        c._update_state({"home_flag": (1 << FLOW_BIT_HOME) | (1 << PA_BIT_HOME)})

        assert c.state.is_support_auto_flow_calibration is True
        assert c.state.is_support_pa_calibration is True

    def test_the_two_fields_use_different_bits(self) -> None:
        """The regression in one assertion: bits 15/16 mean nothing in ``fun``,
        and 6/7 mean nothing in ``home_flag``. The old code mixed them."""
        c = _client("X1C")
        c._update_state({"fun": hex((1 << FLOW_BIT_HOME) | (1 << PA_BIT_HOME))})

        assert c.state.is_support_auto_flow_calibration is False
        assert c.state.is_support_pa_calibration is False

    def test_a_top_level_func_is_ignored(self) -> None:
        """BS reads no such field. Honouring it meant inventing capability out
        of a key nobody sends."""
        c = _client("X1C")
        c._update_state({"func": (1 << 15) | (1 << 16)})

        assert c.state.is_support_auto_flow_calibration is False
        assert c.state.is_support_pa_calibration is False

    def test_support_is_withdrawn_when_the_bit_clears(self) -> None:
        """Assign, not OR. A printer that stops advertising a capability must
        stop having it — otherwise one stray push pins it on forever."""
        c = _client("X1C")
        c._update_state({"fun": hex((1 << FLOW_BIT_FUN) | (1 << PA_BIT_FUN))})
        assert c.state.is_support_pa_calibration is True

        c._update_state({"fun": "0"})
        assert c.state.is_support_pa_calibration is False
        assert c.state.is_support_auto_flow_calibration is False

    def test_fun_wins_over_home_flag(self) -> None:
        """BS parses flag first and fun after, so the new protocol has the last
        word on a printer that sends both."""
        c = _client("X1C")
        c._update_state({"home_flag": (1 << PA_BIT_HOME), "fun": "0"})

        assert c.state.is_support_pa_calibration is False


class TestBambusOwnClamps:
    @pytest.mark.parametrize("model", ["H2D", "H2D Pro", "H2C", "H2S"])
    def test_the_o_series_never_reports_flow_support(self, model: str) -> None:
        c = _client(model)
        c._update_state({"fun": hex((1 << FLOW_BIT_FUN) | (1 << PA_BIT_FUN))})

        assert c.state.is_support_auto_flow_calibration is False, "H2D pushes a flow bit it does not honour"
        assert c.state.is_support_pa_calibration is True, "only flow is clamped on this series"

    @pytest.mark.parametrize("model", ["P1S", "P1P"])
    def test_the_p_series_never_reports_pa_support(self, model: str) -> None:
        c = _client(model)
        c._update_state({"fun": hex((1 << FLOW_BIT_FUN) | (1 << PA_BIT_FUN))})

        assert c.state.is_support_pa_calibration is False
        assert c.state.is_support_auto_flow_calibration is True

    def test_the_x2d_is_not_clamped_despite_looking_like_an_h2(self) -> None:
        """``N6.json`` says ``series_x1``. A clamp keyed on the model name would
        have caught the X2D and silently hidden a capability it does have."""
        c = _client("X2D")
        c._update_state({"fun": hex((1 << FLOW_BIT_FUN) | (1 << PA_BIT_FUN))})

        assert c.state.is_support_auto_flow_calibration is True
        assert c.state.is_support_pa_calibration is True

    def test_the_clamp_runs_after_the_legacy_source_too(self) -> None:
        """BS clamps at BOTH parse sites. A source that skipped it would
        re-enable what the other refused."""
        c = _client("H2D")
        c._update_state({"home_flag": (1 << FLOW_BIT_HOME)})

        assert c.state.is_support_auto_flow_calibration is False

    def test_an_unknown_model_is_not_clamped(self) -> None:
        c = _client("DefinitelyNotABambu")
        c._update_state({"fun": hex((1 << FLOW_BIT_FUN) | (1 << PA_BIT_FUN))})

        assert c.state.is_support_auto_flow_calibration is True
        assert c.state.is_support_pa_calibration is True


class TestTheHexHelper:
    def test_it_takes_strings_and_ints(self) -> None:
        assert parse_hex_bitfield("c0") == 0xC0
        assert parse_hex_bitfield(0xC0) == 0xC0

    def test_absent_and_unparseable_answer_none_not_zero(self) -> None:
        """Zero means "reported, all bits clear" — a different thing from "not
        reported", and the caller must be able to tell them apart."""
        assert parse_hex_bitfield(None) is None
        assert parse_hex_bitfield("zzz") is None
        assert parse_hex_bitfield("0") == 0
