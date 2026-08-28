"""A stagger slot has to survive the gap between dispatch and the print starting.

**The incident (farm logs, 2026-08-19).** Stagger configured for 2 concurrent
starts, 2-minute interval. The operator cleared the plates on eleven printers;
all eleven began heating their beds inside one 1.8-second scheduler tick, and
the mains protection was the thing that noticed.

**Why.** A printer that has been dispatched to still reports the PREVIOUS
print's terminal state — the logs show every one of them at ``FINISH``, and
``on_print_start`` arriving 8 to 28 seconds later. ``_cleanup_stagger_slots``
read that as "this printer's print is over" and released the slot. Because
``acquire_stagger_slot`` re-runs that cleanup on every acquire, each dispatch
evicted the slots taken by the dispatches before it:

    Stagger: printer 2 started (interval=120s), 2 slots occupied
    Stagger: printer 2 started (interval=120s), 1 slots occupied   <- evicted
    Stagger: printer 3 started (interval=120s), 2 slots occupied
    Stagger: printer 3 started (interval=120s), 1 slots occupied   <- again

The count never reached the cap, so the gate never blocked.

⚠️ The eviction itself is not wrong — a slot must not outlive its print. What
was wrong is concluding "over" from a state recorded before the print began.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.services.print_scheduler import _DISPATCH_SETTLE_SECONDS, PrintScheduler


class _FakeManager:
    """Printer states by id, as ``printer_manager.get_status`` reports them."""

    def __init__(self, states: dict[int, str]):
        self._states = states

    def get_status(self, printer_id: int):
        state = self._states.get(printer_id)
        return SimpleNamespace(state=state, temperatures={}, raw_data={}) if state else None

    def get_printer(self, printer_id: int):
        return SimpleNamespace(name=f"P{printer_id}")


@pytest.fixture
def scheduler():
    return PrintScheduler()


def _cleanup(scheduler, states: dict[int, str], *, wait_for_bed: bool = True):
    with patch("backend.app.services.print_scheduler.printer_manager", _FakeManager(states)):
        scheduler._cleanup_stagger_slots(wait_for_bed)


class TestTheIncident:
    def test_a_printer_still_showing_finish_keeps_its_slot(self, scheduler):
        """The exact shape of the failure: dispatched, not yet started."""
        scheduler._register_stagger_start(1, 120)

        _cleanup(scheduler, {1: "FINISH"})

        assert [s.printer_id for s in scheduler._stagger_slots] == [1]

    def test_the_cap_holds_across_a_burst_of_dispatches(self, scheduler):
        """Eleven printers, cap of two — the count must stop at the cap however
        many acquires run the cleanup in between."""
        states = dict.fromkeys(range(1, 12), "FINISH")

        started = []
        for printer_id in range(1, 12):
            _cleanup(scheduler, states)  # every acquire re-runs this
            if scheduler._can_start_staggered(2):
                scheduler._register_stagger_start(printer_id, 120)
                started.append(printer_id)

        assert started == [1, 2], "only the cap may start; the rest wait"
        assert len(scheduler._stagger_slots) == 2

    def test_a_failed_previous_print_is_the_same_case(self, scheduler):
        """Printer 11 was at FAILED, not FINISH, and started anyway."""
        scheduler._register_stagger_start(11, 120)

        _cleanup(scheduler, {11: "FAILED"})

        assert len(scheduler._stagger_slots) == 1


class TestTheSlotStillEndsWhenItShould:
    def test_once_the_print_is_seen_running_a_terminal_state_releases_it(self, scheduler):
        """The eviction this guard narrows must still work — otherwise a slot
        outlives its print and the farm stops instead of starting twice."""
        scheduler._register_stagger_start(1, 120)

        _cleanup(scheduler, {1: "RUNNING"})  # the print is really under way
        assert len(scheduler._stagger_slots) == 1
        _cleanup(scheduler, {1: "FINISH"})  # and now it is really over

        assert scheduler._stagger_slots == []

    def test_a_dispatch_that_never_starts_cannot_hold_a_slot_for_ever(self, scheduler):
        """Bounded, or one wedged printer parks a slot until a restart."""
        scheduler._register_stagger_start(1, 120)
        scheduler._stagger_slots[0].started_at = time.monotonic() - _DISPATCH_SETTLE_SECONDS - 1

        _cleanup(scheduler, {1: "FINISH"})

        assert scheduler._stagger_slots == []

    def test_a_running_printer_holds_its_slot_while_the_bed_heats(self, scheduler):
        scheduler._register_stagger_start(1, 120)

        _cleanup(scheduler, {1: "RUNNING"})

        assert len(scheduler._stagger_slots) == 1, "temp_reached_at is still None — still heating"

    def test_an_idle_printer_keeps_its_slot(self, scheduler):
        """IDLE was already on the keep list and stays there: a printer that has
        taken the command and gone idle-before-prepare is not finished."""
        scheduler._register_stagger_start(1, 120)

        _cleanup(scheduler, {1: "IDLE"})

        assert len(scheduler._stagger_slots) == 1

    def test_an_unknown_printer_is_left_alone(self, scheduler):
        """No status at all (not connected yet) is not evidence of anything."""
        scheduler._register_stagger_start(1, 120)

        _cleanup(scheduler, {})

        assert len(scheduler._stagger_slots) == 1
