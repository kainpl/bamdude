"""Every command's verdict, read in one place — and the sequence-id band that
makes "is this reply mine?" answerable at all.

Audit item 21. BS runs one check for the whole protocol (``DeviceManager.cpp``):
a reply carrying ``command`` and a numeric ``err_code``, whose ``sequence_id``
passes ``is_studio_cmd``/``is_cloud_cmd``, goes to
``add_command_error_code_dlg``. We had one reader, for ``set_ctt``; every other
command we publish went out and its answer was dropped, so a refusal and a
success looked the same from here.

The band matters as much as the router. We counted from 0 — inside nobody's
range and outside our own — so a reply with ``sequence_id`` 5 could equally be
ours or the printer screen's. One place already needed the answer and hardcoded
it: ``project_file`` pins "20000", which is BS's ``STUDIO_START_SEQ_ID`` exactly.
This turns that single literal into the rule it was an instance of.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import STUDIO_SEQ_END, STUDIO_SEQ_START, BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="H2D")
    c._client = MagicMock()
    c.state.connected = True
    return c


class TestTheBandIsBambuStudios:
    def test_the_bounds_are_bs_own(self) -> None:
        """``DevUtil.h``: STUDIO_START_SEQ_ID 20000, STUDIO_END_SEQ_ID 30000."""
        assert (STUDIO_SEQ_START, STUDIO_SEQ_END) == (20000, 30000)

    def test_a_fresh_client_starts_in_the_band(self) -> None:
        assert _client()._sequence_id == STUDIO_SEQ_START

    def test_the_first_command_does_not_take_the_project_file_id(self) -> None:
        """20000 is pinned by ``project_file`` so a slicer-launched print can be
        told from ours. An ordinary command must never land on it."""
        c = _client()
        c._sequence_id += 1

        assert c._sequence_id == STUDIO_SEQ_START + 1

    def test_it_wraps_instead_of_leaving_the_band(self) -> None:
        """⚠️ BS does not wrap: ``m_sequence_id`` is a static int incremented
        forever, so after 10 000 commands its own replies stop passing
        ``is_studio_cmd``. Survivable in a desktop session, certain in a server
        that stays up for weeks."""
        c = _client()
        c._sequence_id = STUDIO_SEQ_END - 1
        c._sequence_id += 1

        assert c._sequence_id == STUDIO_SEQ_START + 1

    def test_the_wrap_skips_the_reserved_id(self) -> None:
        c = _client()
        c._sequence_id = STUDIO_SEQ_END - 1
        c._sequence_id += 1

        assert c._sequence_id != STUDIO_SEQ_START


class TestWhoseReplyIsIt:
    @pytest.mark.parametrize("seq", [STUDIO_SEQ_START, STUDIO_SEQ_START + 1, STUDIO_SEQ_END - 1, "20345"])
    def test_ours(self, seq) -> None:
        assert _client()._is_our_sequence_id(seq) is True

    def test_cloud_zero_counts_as_ours(self) -> None:
        """BS ``is_cloud_cmd`` — and our own ``stop`` publishes "0"."""
        assert _client()._is_our_sequence_id("0") is True

    @pytest.mark.parametrize("seq", [1, 5, 19999, STUDIO_SEQ_END, 99999])
    def test_not_ours(self, seq) -> None:
        assert _client()._is_our_sequence_id(seq) is False

    @pytest.mark.parametrize("seq", [None, "", "abc", {}])
    def test_unreadable_is_not_ours(self, seq) -> None:
        assert _client()._is_our_sequence_id(seq) is False


class TestTheRouterRecordsRealFailures:
    def test_a_failed_command_is_recorded(self) -> None:
        c = _client()

        c._process_message({"print": {"command": "print_option", "err_code": 0x0500_8061, "sequence_id": "20345"}})

        err = c.state.last_command_error
        assert err is not None
        assert err["command"] == "print_option"
        assert err["short_code"] == "0500_8061"
        assert err["sequence_id"] == "20345"
        assert err["at"]

    def test_it_covers_commands_that_had_no_reader_of_their_own(self) -> None:
        """The point of a router: this command has no per-command branch, and
        before it existed its refusal was simply lost."""
        c = _client()

        c._process_message({"print": {"command": "ams_filament_setting", "err_code": 1, "sequence_id": "20001"}})

        assert c.state.last_command_error["command"] == "ams_filament_setting"

    def test_the_latest_verdict_replaces_the_previous(self) -> None:
        c = _client()

        c._process_message({"print": {"command": "a", "err_code": 1, "sequence_id": "20001"}})
        c._process_message({"print": {"command": "b", "err_code": 2, "sequence_id": "20002"}})

        assert c.state.last_command_error["command"] == "b"

    def test_it_does_not_pollute_the_hms_list(self) -> None:
        """``print_error`` entries clear when the printer stops reporting the
        condition. A command error is one-shot, so parking it there would leave
        a fault on the card that nothing can ever take off."""
        c = _client()

        c._process_message({"print": {"command": "print_option", "err_code": 0x0500_8061, "sequence_id": "20345"}})

        assert c.state.hms_errors == []


class TestTheRouterStaysQuiet:
    @pytest.mark.parametrize("err", [0, -1, -2])
    def test_zero_and_negatives_are_not_errors(self, err: int) -> None:
        """BS: ``command_err > 0``. This channel is a status word, not a return
        value — ``set_ctt``'s informative codes are negative and live on a
        different field (``errno``) for exactly that reason."""
        c = _client()

        c._process_message({"print": {"command": "print_option", "err_code": err, "sequence_id": "20345"}})

        assert c.state.last_command_error is None

    def test_a_strangers_command_is_not_ours_to_report(self) -> None:
        """The topic is shared with the printer's screen, the app and the cloud.
        Reporting their failure would name a fault the operator did not cause."""
        c = _client()

        c._process_message({"print": {"command": "print_option", "err_code": 5, "sequence_id": "7"}})

        assert c.state.last_command_error is None

    def test_a_reply_without_err_code_is_not_a_failure(self) -> None:
        c = _client()

        c._process_message({"print": {"command": "push_status", "sequence_id": "20345"}})

        assert c.state.last_command_error is None

    def test_a_non_numeric_err_code_is_ignored(self) -> None:
        c = _client()

        c._process_message({"print": {"command": "print_option", "err_code": "boom", "sequence_id": "20345"}})

        assert c.state.last_command_error is None

    def test_set_ctt_keeps_its_own_mechanism(self) -> None:
        """Two different fields on two different contracts. ``errno`` -2 must
        still clear the optimistic chamber target, and must not be mistaken for
        an ``err_code``."""
        c = _client()
        c.set_chamber_temperature(50)

        c._process_message({"print": {"command": "set_ctt", "errno": -2, "sequence_id": "20345"}})

        assert c.state.temperatures["chamber_target"] == 0.0
        assert c.state.last_command_error is None
