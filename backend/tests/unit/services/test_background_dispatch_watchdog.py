"""Regression tests for ``BackgroundDispatchService._verify_print_response`` (#1134).

Ports upstream Bambuddy's `test_background_dispatch_watchdog.py` (audit cycle
v0.2.3.2 → v0.2.4b1, item A.5).

The background-dispatch watchdog used to be fire-and-forget — it logged a
warning and force-reconnected MQTT, but the dispatch job had already been
marked successful. The user therefore saw "Print started successfully" while
the printer never actually transitioned (HMS error pending, half-broken MQTT
session, plate-clear gate, SD card fault). The watchdog now returns a bool
so the caller can fail the dispatch job when the printer doesn't acknowledge
the command, mirroring what `_watchdog_print_start` does on the queue side.

Both transition signals are accepted: ``state`` advancing past ``pre_state``
*or* ``subtask_id`` advancing past ``pre_subtask_id`` — H2D firmware can sit
at FINISH for ~50 s after accepting ``project_file`` while echoing the new
subtask_id back almost immediately (#1078).

The integration-level `_run_reprint_archive` / `_run_print_library_file`
wiring tests in upstream were extremely mock-heavy and tightly coupled to
upstream's internal structure; here we cover the contract change (bool
return + new pre_subtask_id arg) at the unit level and pin the
`_run_active_job → _mark_job_finished(failed=True)` propagation directly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.background_dispatch import (
    BackgroundDispatchService,
    PrintDispatchJob,
)


def _status(state: str, subtask_id: str | None = None):
    """Minimal stand-in for PrinterState — only the fields the watchdog reads."""
    return SimpleNamespace(state=state, subtask_id=subtask_id, gcode_file=None)


class TestReturnsTrueOnPickup:
    @pytest.mark.asyncio
    async def test_returns_true_on_state_change(self):
        get_status = MagicMock(return_value=_status("RUNNING", "OLD_SUBTASK"))
        with patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            get_status,
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.3,
                poll_interval=0.05,
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_on_subtask_id_change_even_if_state_still_finish(self):
        """#1078: H2D keeps state=FINISH for ~50 s after accepting project_file
        but flips subtask_id immediately. Must be accepted as a pickup signal."""
        get_status = MagicMock(return_value=_status("FINISH", "NEW_SUBTASK_12345"))
        with patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            get_status,
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="H2D",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK_99999",
                timeout=0.3,
                poll_interval=0.05,
            )

        assert result is True


class TestRejectsInactiveTransitions:
    """B.3 / upstream Bambuddy #1370 regression — narrow "command landed" to
    an allow-list of active-print states so FINISH→IDLE (user dismissing a
    post-print prompt) does NOT register as a successful dispatch."""

    @pytest.mark.asyncio
    async def test_returns_false_on_finish_to_idle_user_dismissed_prompt(self):
        """When pre_state is FINISH and the printer transitions to IDLE during
        the verifier window, that's the user dismissing a post-print prompt —
        NOT acceptance of our project_file. The old `state != pre_state`
        check incorrectly returned True; the dispatch job was marked
        successful even though no print was running.
        """
        get_status = MagicMock(return_value=_status("IDLE", "OLD_SUBTASK"))
        client = MagicMock()
        get_client = MagicMock(return_value=client)

        with (
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_status",
                get_status,
            ),
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_client",
                get_client,
            ),
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.2,
                poll_interval=0.05,
            )

        assert result is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("active_state", ["PREPARE", "SLICING", "RUNNING", "PAUSE"])
    async def test_returns_true_on_each_active_print_state(self, active_state):
        """All four active-print states must be accepted as "command landed"."""
        get_status = MagicMock(return_value=_status(active_state, "OLD_SUBTASK"))
        with patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            get_status,
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.3,
                poll_interval=0.05,
            )

        assert result is True, f"{active_state} should be treated as 'command landed'"


class TestReturnsFalseOnTimeout:
    @pytest.mark.asyncio
    async def test_returns_false_when_neither_state_nor_subtask_id_changes(self):
        """The exact #1134 scenario: P1S sits in FAILED with HMS pending,
        accepts the MQTT publish, never transitions. Watchdog must report
        failure so the caller fails the dispatch job."""
        get_status = MagicMock(return_value=_status("FINISH", "OLD_SUBTASK"))
        client = MagicMock()
        get_client = MagicMock(return_value=client)

        with (
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_status",
                get_status,
            ),
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_client",
                get_client,
            ),
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.2,
                poll_interval=0.05,
            )

        assert result is False
        client.force_reconnect_stale_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_pre_subtask_id_none_and_state_unchanged(self):
        """Backward-compat: callers without a captured pre_subtask_id (e.g. the
        printer never reported one) must still get the timeout failure path
        based on state alone."""
        get_status = MagicMock(return_value=_status("FINISH", "ANYTHING"))
        get_client = MagicMock(return_value=None)

        with (
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_status",
                get_status,
            ),
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_client",
                get_client,
            ),
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id=None,
                timeout=0.2,
                poll_interval=0.05,
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_subtask_id_none_post_dispatch_does_not_count_as_change(self):
        """If the printer transiently reports subtask_id=None during the
        watchdog window (e.g. mid-reconnect), that must not be treated as
        "advanced past pre_subtask_id" — otherwise we'd false-pass and mark
        a never-started print as successful."""
        get_status = MagicMock(return_value=_status("FINISH", None))
        get_client = MagicMock(return_value=None)

        with (
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_status",
                get_status,
            ),
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_client",
                get_client,
            ),
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.2,
                poll_interval=0.05,
            )

        assert result is False


class TestDisconnectHandling:
    @pytest.mark.asyncio
    async def test_disconnect_does_not_short_circuit_window(self):
        """A momentary ``get_status() is None`` (brief MQTT disconnect mid-window)
        must not immediately fail the dispatch — the printer may reconnect and
        still produce a valid transition before timeout."""
        get_status = MagicMock(side_effect=[None, _status("RUNNING")])
        with patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            get_status,
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.3,
                poll_interval=0.05,
            )

        assert result is True
        assert get_status.call_count >= 2

    @pytest.mark.asyncio
    async def test_disconnect_for_full_window_returns_false(self):
        """Persistent disconnect for the full window is treated as failure.
        Better to false-fail and let the user retry than to false-succeed and
        leave them watching an idle printer (#1134)."""
        get_status = MagicMock(return_value=None)
        get_client = MagicMock(return_value=None)

        with (
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_status",
                get_status,
            ),
            patch(
                "backend.app.services.background_dispatch.printer_manager.get_client",
                get_client,
            ),
        ):
            result = await BackgroundDispatchService._verify_print_response(
                printer_id=42,
                printer_name="P1S",
                pre_state="FINISH",
                pre_subtask_id="OLD_SUBTASK",
                timeout=0.2,
                poll_interval=0.05,
            )

        assert result is False


class TestDefaults:
    def test_default_timeout_matches_queue_watchdog(self):
        """Queue and background watchdogs need the same 90 s default to give
        slow H2D FINISH→PREPARE transitions the same headroom on both paths."""
        import inspect

        sig = inspect.signature(BackgroundDispatchService._verify_print_response)
        assert sig.parameters["timeout"].default == 90.0


class TestActiveJobFailurePropagation:
    """When `_process_job` raises RuntimeError (the new-A.5 path on watchdog
    timeout), `_run_active_job` must mark the dispatch job failed — this is
    what surfaces "Print did not start" to the UI instead of "Print started
    successfully" (the pre-#1134 silent-success bug)."""

    @pytest.mark.asyncio
    async def test_run_active_job_marks_failed_on_runtime_error(self):
        service = BackgroundDispatchService()
        job = PrintDispatchJob(
            id=1,
            kind="reprint_archive",
            source_id=99,
            source_name="Test.gcode.3mf",
            printer_id=10,
            printer_name="P1S",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )

        with (
            patch.object(
                service,
                "_process_job",
                AsyncMock(side_effect=RuntimeError("Printer did not acknowledge print command — state still FINISH.")),
            ),
            patch.object(service, "_mark_job_finished", AsyncMock()) as mark_finished,
        ):
            await service._run_active_job(job)

        mark_finished.assert_awaited_once()
        kwargs = mark_finished.await_args.kwargs
        assert kwargs["failed"] is True
        assert "did not acknowledge" in kwargs["message"]
