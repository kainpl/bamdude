"""A drying cycle must not report itself finished a minute in.

Ported from upstream #2759. Starting the dryer on an AMS 2 Pro holding two PETG
and two PLA spools and picking PLA showed "PLA @ 45 °C" for about a minute and
then "PETG @ 65 °C" for the remaining twelve hours.

Between accepting the command and settling its countdown the firmware publishes
one update carrying a remaining time of **zero** while the unit is still in its
Checking phase — the reporter's log has 720, then 0, then 719, with the info hex
decoding to dry_status 2 (Drying) four seconds later.

⚠️ Two separate things read that zero, and both were wrong:

- the falling-edge detector fired ``on_drying_complete``, which is what
  schedules smart-plug auto-off — power cut one minute into a twelve-hour dry;
- the cached-target purge dropped the target we recorded when the command went
  out, leaving the badge to guess from the trays for the rest of the cycle.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.bambu_mqtt import _ACTIVE_DRY_STATUSES
from backend.app.services.printer_manager import uniform_tray_drying_hint

CHECKING, DRYING, COOLING, STOPPING, ERROR, STUCK_HEATER = 1, 2, 3, 4, 5, 6


def _info(dry_status: int) -> str:
    """An AMS ``info`` hex whose bits 4-7 carry the phase, as BS parses it."""
    return f"{dry_status << 4:x}"


@pytest.fixture
def client():
    """The real constructor — it opens no socket, and a hand-built instance
    just means chasing whichever attribute the merge path touches next."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    return BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
        on_drying_complete=MagicMock(),
    )


def _push(client, *, dry_time: int, dry_status: int, ams_id: int = 0) -> None:
    """One AMS update, through the real merge path."""
    client._handle_ams_data({"ams": [{"id": str(ams_id), "dry_time": dry_time, "info": _info(dry_status)}]})


class TestTheTransientZero:
    def test_a_zero_while_checking_does_not_end_the_cycle(self, client):
        _push(client, dry_time=720, dry_status=CHECKING)
        _push(client, dry_time=0, dry_status=CHECKING)

        client.on_drying_complete.assert_not_called()

    def test_and_the_real_end_still_lands_afterwards(self, client):
        """⚠️ The suppressed edge must leave the remembered value alone, or the
        push that really ends the cycle sees a previous of 0 and no edge at
        all — the bug would be traded for a quieter one."""
        _push(client, dry_time=720, dry_status=CHECKING)
        _push(client, dry_time=0, dry_status=CHECKING)  # the transient
        _push(client, dry_time=719, dry_status=DRYING)
        _push(client, dry_time=0, dry_status=0)  # Off — the genuine end

        client.on_drying_complete.assert_called_once_with(0)

    def test_the_cached_target_survives_the_transient(self, client):
        """This is the half the reporter actually saw: losing the cache left the
        badge guessing for the remaining eleven hours."""
        client._drying_targets[0] = {"filament": "PLA", "temp": 45}

        _push(client, dry_time=720, dry_status=CHECKING)
        _push(client, dry_time=0, dry_status=CHECKING)

        assert client._drying_targets[0] == {"filament": "PLA", "temp": 45}

    def test_the_cache_is_still_dropped_when_the_cycle_really_ends(self, client):
        client._drying_targets[0] = {"filament": "PLA", "temp": 45}

        _push(client, dry_time=720, dry_status=DRYING)
        _push(client, dry_time=0, dry_status=0)

        assert 0 not in client._drying_targets


class TestWhichPhasesCountAsLive:
    @pytest.mark.parametrize("phase", [CHECKING, DRYING, COOLING])
    def test_a_live_phase_suppresses_the_edge(self, client, phase):
        _push(client, dry_time=720, dry_status=DRYING)
        _push(client, dry_time=0, dry_status=phase)

        client.on_drying_complete.assert_not_called()

    @pytest.mark.parametrize("phase", [STOPPING, ERROR, STUCK_HEATER])
    def test_an_ending_phase_ends_the_cycle(self, client, phase):
        """⚠️ Deliberately NOT BS's ``AmsIsDrying()``, which counts Error and a
        stuck heater as "drying" because it answers a different question — what
        the UI should show. Ours is "has the cycle ended", and those are
        endings; a stuck heater is one we especially want the auto-off to see."""
        _push(client, dry_time=720, dry_status=DRYING)
        _push(client, dry_time=0, dry_status=phase)

        client.on_drying_complete.assert_called_once_with(0)

    def test_a_unit_reporting_no_phase_at_all_still_ends_its_cycles(self, client):
        """The gate may only ever suppress on positive evidence that the cycle
        is live — a firmware that reports no phase must not dry for ever."""
        client._handle_ams_data({"ams": [{"id": "0", "dry_time": 720}]})
        client._handle_ams_data({"ams": [{"id": "0", "dry_time": 0}]})

        client.on_drying_complete.assert_called_once_with(0)

    def test_the_live_set_is_the_three_phases_it_should_be(self):
        assert {CHECKING, DRYING, COOLING} == _ACTIVE_DRY_STATUSES


class TestGuessingFromTheTrays:
    def test_a_mixed_unit_answers_nothing(self):
        """⚠️ The reporter's case. Slot 1 held PETG and the cycle was PLA; the
        badge stated a temperature the cycle was not using."""
        assert uniform_tray_drying_hint([("PETG", 65), ("PETG", 65), ("PLA", 45), ("PLA", 45)]) == (None, None)

    def test_a_uniform_unit_answers(self):
        assert uniform_tray_drying_hint([("PLA", 45), ("PLA", 45)]) == ("PLA", 45)

    def test_empty_slots_are_not_a_second_filament(self):
        assert uniform_tray_drying_hint([("PLA", 45), ("", None), (None, None)]) == ("PLA", 45)

    def test_the_temperature_comes_from_the_first_slot_that_has_one(self):
        """⚠️ A third-party spool in slot 1 carries no RFID temperature, and
        giving up there threw away what the identical Bambu spool in slot 2 was
        holding."""
        assert uniform_tray_drying_hint([("PLA", None), ("PLA", 45)]) == ("PLA", 45)

    def test_a_filament_with_no_temperature_anywhere_still_names_itself(self):
        assert uniform_tray_drying_hint([("PLA", None), ("PLA", 0)]) == ("PLA", None)

    def test_an_empty_unit_answers_nothing(self):
        assert uniform_tray_drying_hint([]) == (None, None)

    def test_an_unparseable_temperature_falls_through_to_the_next_slot(self):
        assert uniform_tray_drying_hint([("PLA", "warm"), ("PLA", 45)]) == ("PLA", 45)


class TestTheRequestTopicReachesTheCapture:
    """⚠️ The request topic carries every command travelling TO the printer,
    Bambu Studio's included — and it used to return before the logging block, so
    a capture could show only what the printer said, never what it was told.
    That is why "what does Studio actually put in the drying command?" had no
    answer from a user's own log.
    """

    def test_a_command_on_the_request_topic_is_recorded(self, client):
        client.enable_logging(True)

        client._on_message(
            None,
            None,
            SimpleNamespace(
                topic=client.topic_publish,
                payload=b'{"print": {"command": "ams_filament_setting", "sequence_id": "1"}}',
            ),
        )

        entries = client.get_logs()
        assert len(entries) == 1
        assert entries[0].topic == client.topic_publish

    def test_it_is_filed_as_outbound(self):
        """Grouped with our own commands rather than with printer telemetry —
        the direction is about where the message was going, not who saw it."""
        import inspect

        from backend.app.services.bambu_mqtt import BambuMQTTClient

        source = inspect.getsource(BambuMQTTClient._on_message)
        block = source[source.index("if msg.topic == self.topic_publish:") :]
        assert 'direction="out"' in block[: block.index("self._handle_request_message")]

    def test_nothing_is_recorded_while_logging_is_off(self, client):
        client._on_message(
            None,
            None,
            SimpleNamespace(topic=client.topic_publish, payload=b'{"print": {"command": "gcode_line"}}'),
        )

        assert client.get_logs() == []
