"""Firmware operations answer in their own envelope, and nobody was reading it.

Audit item 22. BS keeps two copies of the command-error check because the
printer replies to firmware operations under ``upgrade`` rather than ``print``
(``DeviceManager.cpp``). Item 21 covered the first; this is the second.

It matters more than a missing log line. ``ams_firmware_switch`` latches
``ams_firmware_status = "SWITCHING"`` the instant the publish succeeds — copying
BS, which hides its picker the same way — and only a *report* from the printer
ever clears it. A refusal is not a report. So a single declined switch left the
AMS type picker hidden and the endpoint answering 409 "already in progress" for
the life of the process, with nothing on the way that could say otherwise.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import STUDIO_SEQ_START, BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="A1")
    c._client = MagicMock()
    c.state.connected = True
    return c


def _switching() -> BambuMQTTClient:
    """A client that has just published a switch and is holding the latch."""
    c = _client()
    c.ams_firmware_switch(1)
    assert c.state.ams_firmware_status == "SWITCHING"
    return c


class TestTheUpgradeEnvelopeIsRead:
    def test_a_failed_upgrade_command_is_recorded(self) -> None:
        c = _client()

        c._process_message(
            {"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 0x0700_8001, "sequence_id": "20345"}}
        )

        err = c.state.last_command_error
        assert err is not None
        assert err["command"] == "mc_for_ams_firmware_upgrade"
        assert err["short_code"] == "0700_8001"

    def test_a_reply_without_a_sequence_id_still_counts(self) -> None:
        """⚠️ Opposite default to the ``print`` branch, and BS's own choice:
        there ``is_studio_cmd`` must pass, here ``check_studio_cmd`` starts true
        and is only cleared by a sequence id outside the band."""
        c = _client()

        c._process_message({"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 5}})

        assert c.state.last_command_error is not None

    def test_a_strangers_sequence_id_is_still_refused(self) -> None:
        c = _client()

        c._process_message({"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 5, "sequence_id": "7"}})

        assert c.state.last_command_error is None

    @pytest.mark.parametrize("err", [0, -1])
    def test_zero_and_negatives_are_not_errors(self, err: int) -> None:
        c = _client()

        c._process_message({"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": err}})

        assert c.state.last_command_error is None

    def test_an_upgrade_message_with_no_command_is_ignored(self) -> None:
        """``upgrade_state`` arrives under a different key, but a bare
        ``upgrade`` block without a command is not a reply to anything."""
        c = _client()

        c._process_message({"upgrade": {"err_code": 5}})

        assert c.state.last_command_error is None


class TestTheOptimisticLatchIsReleased:
    def test_a_refusal_clears_switching(self) -> None:
        """The defect in one assertion: without this the picker never returns."""
        c = _switching()

        c._process_message(
            {"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 5, "sequence_id": str(c._sequence_id)}}
        )

        assert c.state.ams_firmware_status is None
        assert c.state.ams_firmware_idx_sel is None
        assert "ams_firmware_switch" not in c.state.ams_settings_hold

    def test_it_does_not_invent_a_status(self) -> None:
        """Cleared to None, not to a guessed value — what the AMS is actually on
        arrives in the next report, and a guess here would race it."""
        c = _switching()

        c._process_message({"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 5}})

        assert c.state.ams_firmware_status is None

    def test_a_successful_reply_leaves_the_latch_alone(self) -> None:
        """Only a refusal releases it. A switch that is genuinely running must
        keep the picker hidden until the printer reports otherwise."""
        c = _switching()

        c._process_message({"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 0}})

        assert c.state.ams_firmware_status == "SWITCHING"

    def test_an_unrelated_upgrade_failure_still_clears_it(self) -> None:
        """The latch is the printer's firmware channel being busy with our
        request. Any refusal on that channel means it is not."""
        c = _switching()

        c._process_message({"upgrade": {"command": "upgrade_confirm", "err_code": 5}})

        assert c.state.ams_firmware_status is None

    def test_nothing_is_cleared_when_no_switch_was_pending(self) -> None:
        c = _client()
        c.state.ams_firmware_status = "IDLE"

        c._process_message({"upgrade": {"command": "mc_for_ams_firmware_upgrade", "err_code": 5}})

        assert c.state.ams_firmware_status == "IDLE"


class TestThePrintRouterIsUnaffected:
    def test_print_replies_still_need_a_sequence_id_in_the_band(self) -> None:
        """The two branches differ on purpose; neither should drift into the
        other's rule."""
        c = _client()

        c._process_message({"print": {"command": "print_option", "err_code": 5}})

        assert c.state.last_command_error is None
