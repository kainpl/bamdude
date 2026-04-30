"""Regression tests for `PrintScheduler` post-dispatch hold layer (#1157).

Ports upstream Bambuddy's `test_scheduler_dispatch_hold.py` (audit cycle
v0.2.3.2 → v0.2.4b1, item A.2). Without the hold, multi-plate batches were
triple-dispatched onto the same H2D Pro within ~60 s while the printer was
still digesting the first project_file (FINISH for 80–210 s before flipping
to PREPARE). The PrinterQueue.status='printing' DB seed alone was empirically
unreliable in that window.

These tests exercise the in-memory hold layer directly: mark a printer as
just-dispatched, verify it's reported as held, simulate state/subtask
transitions, and confirm the hold drops on the right signals.
"""

import time
from unittest.mock import patch

from backend.app.services.print_scheduler import PrintScheduler


class _FakeStatus:
    def __init__(self, state: str | None, subtask_id: str | None = None):
        self.state = state
        self.subtask_id = subtask_id


class TestDispatchHoldBasics:
    def test_mark_records_pre_state_and_subtask(self):
        s = PrintScheduler()
        s._mark_printer_dispatched(5, "FINISH", "task_42")
        entry = s._dispatch_holds[5]
        assert entry[1] == "FINISH"
        assert entry[2] == "task_42"

    def test_mark_with_none_pre_state_uses_empty_sentinel(self):
        s = PrintScheduler()
        s._mark_printer_dispatched(5, None, None)
        # Empty sentinel — won't match any real printer state, so transition
        # detection short-circuits to time-based.
        assert s._dispatch_holds[5][1] == ""

    def test_release_drops_entry(self):
        s = PrintScheduler()
        s._mark_printer_dispatched(5, "FINISH", None)
        s._release_dispatch_hold(5)
        assert 5 not in s._dispatch_holds

    def test_release_unknown_printer_is_noop(self):
        s = PrintScheduler()
        s._release_dispatch_hold(99)  # no error


class TestPrinterInDispatchHold:
    def test_no_hold_returns_false(self):
        s = PrintScheduler()
        assert s._printer_in_dispatch_hold(5) is False

    def test_just_dispatched_returns_true(self):
        s = PrintScheduler()
        s._mark_printer_dispatched(5, "FINISH", None)
        # No transition observed, well within cooldown — held.
        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status", return_value=_FakeStatus("FINISH")
        ):
            assert s._printer_in_dispatch_hold(5) is True

    def test_state_transition_after_min_cooldown_releases(self):
        """State changed from FINISH to PREPARE AND past min cooldown → release."""
        s = PrintScheduler()
        s._dispatch_min_cooldown = 0.01  # tiny cooldown for the test
        s._mark_printer_dispatched(5, "FINISH", None)
        time.sleep(0.02)
        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status", return_value=_FakeStatus("PREPARE")
        ):
            assert s._printer_in_dispatch_hold(5) is False
        assert 5 not in s._dispatch_holds  # entry cleared

    def test_state_transition_inside_min_cooldown_still_held(self):
        """Even when state changed, if we're still inside min cooldown → held.

        H2D's project_file digestion can pulse PREPARE→RUNNING→PREPARE in the
        first second after acceptance; the cooldown prevents a spurious
        early-release double-dispatch.
        """
        s = PrintScheduler()
        s._dispatch_min_cooldown = 60.0  # default
        s._mark_printer_dispatched(5, "FINISH", None)
        # No sleep — well inside the 60 s cooldown
        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status", return_value=_FakeStatus("PREPARE")
        ):
            assert s._printer_in_dispatch_hold(5) is True

    def test_subtask_advance_after_cooldown_releases(self):
        s = PrintScheduler()
        s._dispatch_min_cooldown = 0.01
        s._mark_printer_dispatched(5, "FINISH", "task_old")
        time.sleep(0.02)
        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status",
            return_value=_FakeStatus("FINISH", subtask_id="task_new"),
        ):
            assert s._printer_in_dispatch_hold(5) is False

    def test_status_unavailable_keeps_hold(self):
        """Disconnect (get_status=None) doesn't release — fall through to timeout."""
        s = PrintScheduler()
        s._mark_printer_dispatched(5, "FINISH", None)
        with patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=None):
            assert s._printer_in_dispatch_hold(5) is True
        # Hold still present.
        assert 5 in s._dispatch_holds

    def test_hard_timeout_self_clears(self):
        s = PrintScheduler()
        s._dispatch_max_hold = 0.01
        s._mark_printer_dispatched(5, "FINISH", None)
        time.sleep(0.02)
        # Even with no transition signal, hard timeout drops the hold.
        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status", return_value=_FakeStatus("FINISH")
        ):
            assert s._printer_in_dispatch_hold(5) is False
        assert 5 not in s._dispatch_holds

    def test_no_pre_state_falls_back_to_time_based_hold(self):
        s = PrintScheduler()
        s._dispatch_min_cooldown = 0.01
        # No pre_state (e.g. printer was offline at dispatch time)
        s._mark_printer_dispatched(5, None, None)
        # Inside cooldown → still held
        assert s._printer_in_dispatch_hold(5) is True
        time.sleep(0.02)
        # Past cooldown → released
        assert s._printer_in_dispatch_hold(5) is False
        assert 5 not in s._dispatch_holds


class TestPerPrinterIsolation:
    def test_hold_on_one_printer_does_not_block_another(self):
        s = PrintScheduler()
        s._mark_printer_dispatched(5, "FINISH", None)
        with patch(
            "backend.app.services.print_scheduler.printer_manager.get_status", return_value=_FakeStatus("FINISH")
        ):
            assert s._printer_in_dispatch_hold(5) is True
            assert s._printer_in_dispatch_hold(7) is False
