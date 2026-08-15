"""«Device is busy» на принтері, який саме друкує.

`0500_4004` is the firmware refusing a start-print. Twice in one day it arrived
3–6 seconds after an MQTT reconnect on two different X1C, on machines printing
happily, and woke the operator — nothing of ours had asked for anything (the
scheduler refuses a busy printer eleven seconds earlier in the same log).

⚠️ Two halves, and the second is the reason this is not just a filter: left
uncleared on some models (A1 mini reported) this error cancels the RUNNING job.
So the guard takes it off the printer as well as out of our sight.

⚠️ And it stays visible while IDLE. There it means a start-print was refused —
which is exactly what an operator who just pressed Print needs to know.

Source of the incident: vault "Device busy прилітає після реконекту — джерело
не встановлене". That investigation is still open; the guard moves its only
signal from a Telegram notification into the log, it does not delete it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

DEVICE_BUSY = 0x05004004
OTHER_FAULT = 0x05008061  # an unrelated 0500_* error, to prove the guard is narrow


@pytest.fixture
def client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="00M00A000000000", access_code="00000000")
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(client) -> list[dict]:
    return [json.loads(call.args[1]) for call in client._client.publish.call_args_list]


class TestWhileAPrintIsRunning:
    @pytest.mark.parametrize("state", ["RUNNING", "PREPARE", "PAUSE", "SLICING"])
    def test_it_is_not_shown_anywhere(self, client, state: str) -> None:
        """One list feeds all of it — card badge, error modal, notifications,
        MQTT relay. Never appending is what makes "nowhere" true."""
        client._update_state({"gcode_state": state, "print_error": DEVICE_BUSY})

        assert client.state.hms_errors == []

    def test_it_is_cleared_off_the_printer(self, client) -> None:
        client._update_state({"gcode_state": "RUNNING", "print_error": DEVICE_BUSY})

        assert [p["print"]["command"] for p in _published(client)] == ["clean_print_error"]

    def test_a_real_fault_alongside_it_survives(self, client) -> None:
        """⚠️ The clear is published directly instead of через ``clear_hms_errors``,
        which also empties the local list — that would take an unrelated fault
        down with the noise."""
        client._update_state({"gcode_state": "RUNNING", "print_error": OTHER_FAULT})
        client._update_state({"gcode_state": "RUNNING", "print_error": DEVICE_BUSY})

        assert [e.short_code for e in client.state.hms_errors] == ["0500_8061"]

    def test_a_different_code_is_left_alone(self, client) -> None:
        client._update_state({"gcode_state": "RUNNING", "print_error": OTHER_FAULT})

        assert [e.short_code for e in client.state.hms_errors] == ["0500_8061"]
        assert _published(client) == []


class TestWhileIdle:
    @pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
    def test_the_same_code_is_shown(self, client, state: str) -> None:
        """Idle it is not noise: it is the printer refusing a start-print, and
        the operator who pressed Print is owed the reason."""
        client._update_state({"gcode_state": state, "print_error": DEVICE_BUSY})

        assert [e.short_code for e in client.state.hms_errors] == ["0500_4004"]

    def test_and_nothing_is_sent_to_the_printer(self, client) -> None:
        client._update_state({"gcode_state": "IDLE", "print_error": DEVICE_BUSY})

        assert _published(client) == []


class TestNotFloodingThePrinter:
    def test_a_repeated_report_does_not_repeat_the_command(self, client) -> None:
        """⚠️ print_error is repeated in EVERY push_status until cleared, about
        once a second. Without the interval an unacknowledged clear becomes a
        command storm aimed at a machine that is mid-print."""
        for _ in range(10):
            client._update_state({"gcode_state": "RUNNING", "print_error": DEVICE_BUSY})

        assert len(_published(client)) == 1

    def test_it_tries_again_once_the_interval_has_passed(self, client, monkeypatch) -> None:
        """A clear the printer ignored must be retried — otherwise one lost
        command leaves the fault sitting there for the rest of the print, and on
        some models that cancels the job."""
        from backend.app.services import bambu_mqtt

        client._update_state({"gcode_state": "RUNNING", "print_error": DEVICE_BUSY})
        client._device_busy_cleared_at -= bambu_mqtt._DEVICE_BUSY_CLEAR_INTERVAL + 1
        client._update_state({"gcode_state": "RUNNING", "print_error": DEVICE_BUSY})

        assert len(_published(client)) == 2


class TestWhenTheConnectionIsGone:
    def test_it_still_suppresses_and_does_not_raise(self, client) -> None:
        """The publish is best-effort; the suppression is not. A disconnected
        client that raised here would take the whole status update with it."""
        client._client = None

        client._update_state({"gcode_state": "RUNNING", "print_error": DEVICE_BUSY})

        assert client.state.hms_errors == []


class TestTheCodeIsNotInTheBlanketFilter:
    def test_it_is_kept_out_of_the_unconditional_drop_set(self) -> None:
        """⚠️ _HMS_USER_ACTION_CODES drops a code in every state. Moving this one
        in there would be a one-line "simplification" that silently loses the
        idle case — the only case where the code is worth reading."""
        from backend.app.services.bambu_mqtt import _DEVICE_BUSY_CODE, _HMS_USER_ACTION_CODES

        assert _DEVICE_BUSY_CODE not in _HMS_USER_ACTION_CODES
