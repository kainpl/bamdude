"""A stack entry the operator has seen and chosen to hide stays hidden until the
printer itself drops it.

The firmware owns ``hms[]``: "Clear" sends ``clean_print_error``, which touches
only the scalar register, and a stack entry the printer keeps re-sending cannot
be removed from here at all. A P2S farm carried ``0500_0600_0002_0070`` — a code
Bambu ships with no text anywhere — in every push for weeks; the screen showed
nothing, prints ran, and the card's red pip could not be answered (2026-09-04).

So the answer is local and narrow: one printer, one FULL 16-char code, for as
long as the printer keeps reporting it. The mute expires by itself when the
entry leaves the stack, and the same code later is a new incident that is shown
again. Nothing is hidden by description or by short code — see
``HMSErrorModal.filterKnownHMSErrors`` for what that cost once.
"""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

CAMERA = {"attr": 0x05000600, "code": 0x00020070}  # 0500_0600_0002_0070 — the measured P2S entry
DOOR = {"attr": 0x03009600, "code": 0x00030001}  # 0300_9600_0003_0001 — "The front door is open."
CAMERA_FULL = "0500060000020070"
DOOR_FULL = "0300960000030001"


@pytest.fixture
def client():
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="22EMUTE000000001", access_code="1")
    c.on_hms_mute_expired = MagicMock()
    return c


def _codes(entries) -> list[str]:
    return [e.full_code for e in entries]


def test_a_muted_entry_leaves_hms_errors_and_lands_in_hms_muted(client):
    client.set_muted_hms_codes({CAMERA_FULL})
    client._process_message({"print": {"hms": [CAMERA, DOOR]}})

    assert _codes(client.state.hms_errors) == [DOOR_FULL]
    assert _codes(client.state.hms_muted) == [CAMERA_FULL]


def test_muting_applies_at_once_without_waiting_for_a_push(client):
    client._process_message({"print": {"hms": [CAMERA, DOOR]}})

    assert client.mute_hms(CAMERA_FULL) is True
    assert _codes(client.state.hms_errors) == [DOOR_FULL]
    assert _codes(client.state.hms_muted) == [CAMERA_FULL]

    assert client.unmute_hms(CAMERA_FULL) is True
    assert sorted(_codes(client.state.hms_errors)) == sorted([DOOR_FULL, CAMERA_FULL])
    assert client.state.hms_muted == []


def test_the_mute_expires_when_the_printer_drops_the_entry(client):
    client.set_muted_hms_codes({CAMERA_FULL})
    client._process_message({"print": {"hms": [CAMERA]}})
    assert _codes(client.state.hms_muted) == [CAMERA_FULL]

    # A reboot cleared the stack: the mute goes with the entry it was for.
    client._process_message({"print": {"hms": [DOOR]}})
    assert client.muted_hms_codes == set()
    assert client.state.hms_muted == []
    client.on_hms_mute_expired.assert_called_once_with({CAMERA_FULL})

    # The same code later is a NEW incident and is shown.
    client._process_message({"print": {"hms": [CAMERA]}})
    assert _codes(client.state.hms_errors) == [CAMERA_FULL]


def test_a_push_without_hms_expires_nothing(client):
    client.set_muted_hms_codes({CAMERA_FULL})
    client._process_message({"print": {"hms": [CAMERA]}})
    client._process_message({"print": {"mc_percent": 50}})
    client._process_message({"print": {"print_error": 0}})

    assert client.muted_hms_codes == {CAMERA_FULL}
    assert _codes(client.state.hms_muted) == [CAMERA_FULL]
    client.on_hms_mute_expired.assert_not_called()


def test_only_stack_entries_can_be_muted(client):
    """An 8-char ``print_error`` fault is cleared by the printer, never hidden
    by us — Clear already works for it."""
    assert client.mute_hms("05004030") is False
    client.set_muted_hms_codes({"05004030", CAMERA_FULL})
    assert client.muted_hms_codes == {CAMERA_FULL}

    client._process_message({"print": {"print_error": 0x05004030}})
    assert _codes(client.state.hms_errors) == ["05004030"]


def test_an_expiry_callback_that_raises_does_not_break_parsing(client):
    client.on_hms_mute_expired = MagicMock(side_effect=RuntimeError("db down"))
    client.set_muted_hms_codes({CAMERA_FULL})
    client._process_message({"print": {"hms": [CAMERA]}})

    client._process_message({"print": {"hms": [DOOR]}})  # must not raise
    assert _codes(client.state.hms_errors) == [DOOR_FULL]
