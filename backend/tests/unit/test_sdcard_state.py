"""SD-card health has four states, and a broken card must not read as healthy.

BS ``DevStorage::SdcardState``::

    NO_SDCARD = 0, HAS_SDCARD_NORMAL = 1, HAS_SDCARD_ABNORMAL = 2,
    HAS_SDCARD_READONLY = 3

read from ``aux`` bits 12-13 on the new protocol
(``set_sdcard_state(get_flag_bits(aux, 12, 2))``), and from a plain bool on the
legacy one (``ParseV1_0``: bool -> NORMAL or NO_SDCARD).

Ours was a single bool decided by ``"HAS_SDCARD" in value.upper()`` — a
**substring** test, so ``HAS_SDCARD_ABNORMAL`` and ``HAS_SDCARD_READONLY`` both
matched. A card the printer is actively complaining about read as fine, and the
firmware-update precondition (``sd_card_present``) sent a .bin to it.

``aux`` was also the one member of the cfg/fun/aux/stat quartet nothing read at
all: the latch detected the four and parsed three.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import (
    SDCARD_ABNORMAL,
    SDCARD_NONE,
    SDCARD_NORMAL,
    SDCARD_READONLY,
    BambuMQTTClient,
)


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="X1C")
    c._client = MagicMock()
    return c


class TestTheStringForm:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("HAS_SDCARD_NORMAL", SDCARD_NORMAL),
            ("HAS_SDCARD_ABNORMAL", SDCARD_ABNORMAL),
            ("HAS_SDCARD_READONLY", SDCARD_READONLY),
            ("NO_SDCARD", SDCARD_NONE),
        ],
    )
    def test_each_state_is_distinguished(self, value: str, expected: int) -> None:
        c = _client()
        c._update_state({"sdcard": value})
        assert c.state.sdcard_state == expected

    @pytest.mark.parametrize("value", ["HAS_SDCARD_ABNORMAL", "HAS_SDCARD_READONLY"])
    def test_a_card_the_printer_complains_about_is_not_usable(self, value: str) -> None:
        """The regression: both of these contain "HAS_SDCARD" and so passed the
        old substring test. Neither can take a print or a firmware image."""
        c = _client()
        c._update_state({"sdcard": value})
        assert c.state.sdcard is False


class TestTheLegacyBool:
    def test_true_is_normal_and_false_is_none(self) -> None:
        """BS ``ParseV1_0`` maps the legacy bool to exactly these two."""
        c = _client()
        c._update_state({"sdcard": True})
        assert (c.state.sdcard_state, c.state.sdcard) == (SDCARD_NORMAL, True)

        c._update_state({"sdcard": False})
        assert (c.state.sdcard_state, c.state.sdcard) == (SDCARD_NONE, False)


class TestTheAuxBits:
    @pytest.mark.parametrize("state", [SDCARD_NONE, SDCARD_NORMAL, SDCARD_ABNORMAL, SDCARD_READONLY])
    def test_bits_12_and_13_carry_the_state(self, state: int) -> None:
        c = _client()
        c._update_state({"aux": hex(state << 12)})
        assert c.state.sdcard_state == state
        assert c.state.sdcard is (state == SDCARD_NORMAL)

    def test_neighbouring_bits_do_not_leak_in(self) -> None:
        """Two bits wide. The timelapse-kit flag lives at bit 26 in the same
        field, so a mask that was too wide would read it as a card fault."""
        c = _client()
        c._update_state({"aux": hex((1 << 26) | (SDCARD_NORMAL << 12))})
        assert c.state.sdcard_state == SDCARD_NORMAL

    def test_aux_is_read_at_all(self) -> None:
        """It is the member of the cfg/fun/aux/stat quartet that nothing read —
        the latch saw four fields and parsed three."""
        c = _client()
        c._update_state({"aux": hex(SDCARD_ABNORMAL << 12)})
        assert c.state.sdcard_state == SDCARD_ABNORMAL
