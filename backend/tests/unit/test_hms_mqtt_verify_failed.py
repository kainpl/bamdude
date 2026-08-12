"""The printer says it is refusing our commands, and we act on it (#2732).

A P1S on firmware 01.10.00.00 rejected every control command and reported it:
HMS ``0500-0500-0001-0007``, "MQTT command verification failed". Queries
(``get_version``, ``pushall``) still answer, so the connection looks perfectly
healthy while ``project_file``, ``gcode_line`` and ``ams_change_filament`` are
all dropped — the upload succeeds, the printer echoes our subtask_id, and then
it sits at IDLE forever.

Three things went wrong at once, and each is pinned below:

* the code was received and thrown away, because the meaning lives in the half
  of it the short form discards;
* the developer-mode probe read a non-answer as confirmation, so the support
  bundle of a printer refusing everything said ``developer_mode: pass``;
* dispatch treated the refusal as a stall, spending the full window and two more
  uploads to arrive at a message about SD cards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.background_dispatch import (
    PrintCommandRejectedError,
    _mqtt_commands_rejected,
)
from backend.app.services.bambu_mqtt import HMS_MQTT_VERIFY_FAILED


class TestTheCodeItself:
    def test_the_short_form_cannot_express_it(self) -> None:
        """Why the 16-char form is load-bearing: the meaning lives in attr's low
        half (0500) and code's high half (0001), and MMMM_EEEE keeps neither."""
        attr, code = 0x05000500, 0x00010007
        assert f"{attr:08X}{code:08X}" == HMS_MQTT_VERIFY_FAILED
        short = f"{(attr >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
        assert short == "0500_0007", "the collapsed form that matches nothing in any catalog"


class _FakeState:
    def __init__(self) -> None:
        self.developer_mode: bool | None = None
        self.hms_errors: list = []


class _Client:
    """The three attributes ``_apply_mqtt_verify_state`` touches, and nothing else."""

    def __init__(self) -> None:
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        self.serial_number = "01P00A000000000"
        self.state = _FakeState()
        self._dev_mode_from_hms = False
        self._dev_mode_probed = True
        self._dev_mode_needs_probe = False
        self._apply = BambuMQTTClient._apply_mqtt_verify_state.__get__(self)


class TestTheHmsOutranksTheProbe:
    def test_the_code_forces_developer_mode_false(self) -> None:
        client = _Client()
        client.state.developer_mode = True  # what the probe wrongly concluded

        client._apply(True)

        assert client.state.developer_mode is False
        assert client._dev_mode_from_hms is True

    def test_the_code_going_away_returns_to_unknown_and_re_arms_the_probe(self) -> None:
        """Otherwise enabling Developer Mode and restarting the printer would be
        invisible until BamDude itself restarted."""
        client = _Client()
        client._apply(True)

        client._apply(False)

        assert client.state.developer_mode is None
        assert client._dev_mode_from_hms is False
        assert client._dev_mode_needs_probe is True

    def test_a_false_the_probe_decided_is_left_alone(self) -> None:
        """The latch only ever unwinds itself. A probe that got an explicit
        refusal is evidence in its own right."""
        client = _Client()
        client.state.developer_mode = False  # from the probe, not from HMS

        client._apply(False)

        assert client.state.developer_mode is False
        assert client._dev_mode_needs_probe is False


class TestTheProbeHasThreeOutcomes:
    def _probe(self, payload: dict) -> _FakeState:
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = _Client()
        client._dev_mode_probe_seq = "42"
        client._dev_mode_probe_failures = 0
        client.on_state_change = None
        BambuMQTTClient._handle_dev_mode_probe_response.__get__(client)(payload)
        return client.state

    def test_an_explicit_refusal_disables(self) -> None:
        assert self._probe({"result": "failed", "reason": "verify failed"}).developer_mode is False

    def test_an_explicit_success_enables(self) -> None:
        assert self._probe({"result": "success"}).developer_mode is True

    def test_anything_else_stays_unknown(self) -> None:
        """The reported firmware answers the probe with an empty result while
        refusing everything else. Reading that as a yes is what put
        ``developer_mode: pass`` in the bundle of a printer that had not accepted
        a command all day."""
        assert self._probe({}).developer_mode is None
        assert self._probe({"result": ""}).developer_mode is None
        assert self._probe({"result": "failed", "reason": "something else"}).developer_mode is None


class TestDispatchStopsInsteadOfRetrying:
    def _status_with(self, *full_codes: str):
        status = MagicMock()
        status.hms_errors = [MagicMock(full_code=c) for c in full_codes]
        return status

    def test_the_code_is_recognised(self) -> None:
        assert _mqtt_commands_rejected(self._status_with(HMS_MQTT_VERIFY_FAILED)) is True

    def test_other_faults_are_not_mistaken_for_it(self) -> None:
        assert _mqtt_commands_rejected(self._status_with("0300400000004001")) is False

    def test_a_missing_status_is_not_a_refusal(self) -> None:
        """Called on every watchdog poll, including ticks where the printer is
        momentarily not reporting."""
        assert _mqtt_commands_rejected(None) is False

    def test_an_error_without_a_full_code_is_tolerated(self) -> None:
        """The 8-char print_error path builds HMSError differently."""
        status = MagicMock()
        status.hms_errors = [MagicMock(spec=[])]
        assert _mqtt_commands_rejected(status) is False

    def test_the_error_is_a_runtime_error(self) -> None:
        """Both dispatch paths already raise RuntimeError at this point, so the
        job is failed exactly as before — only the message changes."""
        assert issubclass(PrintCommandRejectedError, RuntimeError)


@pytest.mark.asyncio
class TestTheWatchdogFailsFast:
    async def _run(self, states: list, monkeypatch):
        from backend.app.services import background_dispatch as bd

        it = iter(states)

        def _get_status(_printer_id):
            try:
                return next(it)
            except StopIteration:
                return states[-1]

        monkeypatch.setattr(bd.printer_manager, "get_status", _get_status)
        return await bd.BackgroundDispatchService._verify_print_response(
            1, "P1S", "IDLE", pre_subtask_id="old", timeout=1.0, poll_interval=0.01
        )

    async def test_a_refused_command_raises_instead_of_waiting_out_the_window(self, monkeypatch) -> None:
        idle_and_refusing = MagicMock()
        idle_and_refusing.state = "IDLE"
        idle_and_refusing.subtask_id = "old"
        idle_and_refusing.hms_errors = [MagicMock(full_code=HMS_MQTT_VERIFY_FAILED)]

        with pytest.raises(PrintCommandRejectedError) as excinfo:
            await self._run([idle_and_refusing], monkeypatch)

        assert "Developer Mode" in str(excinfo.value)
        assert "0500-0500-0001-0007" in str(excinfo.value), "name the code the printer's own screen shows"

    async def test_a_running_print_wins_over_a_lingering_hms(self, monkeypatch) -> None:
        """Ordering rule: the check sits after the active-state exit, so a stale
        code left over from an earlier job can never abort a print that is
        visibly running."""
        running_with_stale_hms = MagicMock()
        running_with_stale_hms.state = "RUNNING"
        running_with_stale_hms.subtask_id = "new"
        running_with_stale_hms.hms_errors = [MagicMock(full_code=HMS_MQTT_VERIFY_FAILED)]

        assert await self._run([running_with_stale_hms], monkeypatch) is True
