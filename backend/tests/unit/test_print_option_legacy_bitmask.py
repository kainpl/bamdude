"""``auto_recovery`` ships two fields, and we were sending one.

BS ``command_set_printing_option`` (``DeviceManager.cpp``) puts BOTH the named
bool and a legacy ``option`` bitmask on the same command::

    option        = auto_recovery << PRINT_OP_AUTO_RECOVERY   // the bit is 0
    auto_recovery = auto_recovery

Some firmware revisions reject the command when only one of the two is present.

⚠️ **This was audited once and closed as already-correct, wrongly.** The repo did
contain a helper that sent both — with a comment explaining exactly why — but
nothing called it; the live path is ``_publish_print_option_bool``, which sent
the bool alone. Reading the dead function answered a question about the live one.
The helper is now deleted and its knowledge moved to where the command is
actually built, which is the whole point of audit item 27: dead code that reads
as implemented does not just sit there, it answers questions incorrectly.

Only ``auto_recovery`` gets a bit. BS's builder takes that one flag and nothing
else, and ``PRINT_OP_MAX`` follows it immediately — so no other toggle may
invent one.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import PRINT_OP_AUTO_RECOVERY, BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="H2D")
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(c: BambuMQTTClient) -> dict:
    return json.loads(c._client.publish.call_args[0][1])["print"]


class TestAutoRecoveryCarriesBoth:
    @pytest.mark.parametrize("enabled", [True, False])
    def test_the_named_bool_is_present(self, enabled: bool) -> None:
        c = _client()
        c.print_option_auto_recovery(enabled)

        assert _published(c)["auto_recovery"] is enabled

    @pytest.mark.parametrize(("enabled", "expected"), [(True, 1), (False, 0)])
    def test_the_legacy_bitmask_is_present(self, enabled: bool, expected: int) -> None:
        """The regression in one assertion — this key was simply absent."""
        c = _client()
        c.print_option_auto_recovery(enabled)

        assert _published(c)["option"] == expected

    def test_the_bit_is_bs_own(self) -> None:
        """``DeviceManager.hpp``: ``PRINT_OP_AUTO_RECOVERY = 0``."""
        assert PRINT_OP_AUTO_RECOVERY == 0

    def test_it_is_still_a_print_option_command(self) -> None:
        c = _client()
        c.print_option_auto_recovery(True)
        p = _published(c)

        assert p["command"] == "print_option"
        assert "sequence_id" in p


class TestNoOtherToggleInventsABit:
    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.print_option_sound(True),
            lambda c: c.print_option_filament_tangle(True),
            lambda c: c.print_option_nozzle_blob(True),
        ],
    )
    def test_option_is_absent(self, call) -> None:
        """BS's builder takes ``auto_recovery`` alone. A bitmask on a toggle that
        has no bit would set bit 0 — auto-recovery — as a side effect of
        changing something else entirely."""
        c = _client()
        call(c)

        assert "option" not in _published(c)

    def test_the_int_publisher_has_no_bitmask_either(self) -> None:
        c = _client()
        c.print_option_purify_air(1)

        assert "option" not in _published(c)
