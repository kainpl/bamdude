"""An airduct mode does three things to a fan, and we could only say two.

Registry item F1. BS ``FanControlNew::update_mode`` decides in this order:

    mode id < 0        -> slider, no checks at all (old protocol)
    part in mode.off   -> the label "Off"
    part not in ctrl   -> the label "Auto"
    otherwise          -> slider + on/off toggle

⚠️ The middle case is what a boolean could not carry. Reading only ``off`` meant
every auto fan answered "controllable": the card drew a slider, the command went
out, and the mode overrode it — a control that visibly does nothing. BS's own
``AirMode`` comment states the rule: *"If the fan is not off or ctrl, it will be
displayed as auto"*.

The two states are also different answers for a person: "the mode holds this fan
off" is something you can change by changing the mode; "the firmware is driving
it" is not.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import (
    FAN_AUTO,
    FAN_CTRL,
    FAN_OFF,
    PrinterState,
    airduct_fan_control,
    airduct_fan_controllable,
    airduct_mode_effective,
    airduct_parts_effective,
)


def _state(mode: int | None = 0, ctrl: list[int] | None = None, off: list[int] | None = None) -> PrinterState:
    """A printer that reports a real air duct.

    ⚠️ ``airduct_parts`` has to be non-empty. A machine with mode lists and no
    parts does not exist, and the effective mode is ``-1`` — the old protocol —
    for exactly that shape, which would take the ``mode < 0`` branch and make
    every assertion below trivially "controllable".
    """
    s = PrinterState()
    s.airduct_mode = mode
    s.airduct_parts = {
        pid: {"type": 0, "state": 0, "range_start": 0, "range_end": 100}
        for pid in {2, 3, 10, *(ctrl or []), *(off or [])}
    }
    if ctrl is not None or off is not None:
        s.airduct_modes = {mode: {"ctrl": ctrl or [], "off": off or []}}
    return s


class TestTheThreeOutcomes:
    def test_a_part_the_mode_hands_over_is_controllable(self) -> None:
        assert airduct_fan_control(_state(ctrl=[2], off=[10]), 2) == FAN_CTRL

    def test_a_part_the_mode_forces_off(self) -> None:
        """The P2S's left aux in heating mode."""
        assert airduct_fan_control(_state(ctrl=[2], off=[10]), 10) == FAN_OFF

    def test_a_part_in_neither_list_is_auto(self) -> None:
        """The case the boolean could not express — and the common one: BS lists
        only what it forces off and what it hands over, so anything the firmware
        keeps for itself simply appears in neither."""
        assert airduct_fan_control(_state(ctrl=[2], off=[10]), 3) == FAN_AUTO

    def test_off_is_checked_before_ctrl(self) -> None:
        """BS looks at ``off`` first. A part in both lists is Off, not a
        slider — and firmware that reports both is not ours to arbitrate."""
        assert airduct_fan_control(_state(ctrl=[2], off=[2]), 2) == FAN_OFF


class TestTheWritePathOnlyAcceptsCtrl:
    def test_ctrl_may_be_driven(self) -> None:
        assert airduct_fan_controllable(_state(ctrl=[2], off=[10]), 2) is True

    def test_off_may_not(self) -> None:
        assert airduct_fan_controllable(_state(ctrl=[2], off=[10]), 10) is False

    def test_auto_may_not_either(self) -> None:
        """The regression in one assertion: this used to be True, so a command
        reached a fan the mode owns."""
        assert airduct_fan_controllable(_state(ctrl=[2], off=[10]), 3) is False


class TestWhenThereAreNoListsToConsult:
    def test_the_old_protocol_is_always_controllable(self) -> None:
        """BS's ``cur_mode < 0`` branch shows the slider with no checks — there
        are no mode lists on that protocol, so nothing can forbid the fan."""
        assert airduct_fan_control(_state(mode=-1), 10) == FAN_CTRL

    def test_an_unknown_mode_is_auto(self) -> None:
        """⚠️ BS **does** have an answer here, and it is not "controllable".
        ``AirDuctData::modes`` is a ``std::map`` indexed with ``operator[]``,
        which default-constructs a missing entry: empty ``off``, empty ``ctrl``.
        The part is then in neither list — the auto branch.

        An earlier version of this returned controllable, on the reasoning that
        BS had no answer. It had one; I had not worked it out."""
        s = _state(mode=7)
        s.airduct_modes = {1: {"ctrl": [2], "off": []}}

        assert airduct_fan_control(s, 10) == FAN_AUTO

    def test_a_printer_with_no_airduct_takes_the_old_protocol_branch(self) -> None:
        """No parts means the fans were synthesised, and ``converse_to_duct``
        stamps mode ``-1`` on exactly that case — so the gate never consults a
        list, as in BS."""
        s = PrinterState()
        s.airduct_mode = 0

        assert airduct_fan_control(s, 10) == FAN_CTRL


class TestWhichProtocolWeAreOn:
    """BS decides by ``modes.empty()``, not by whether parts arrived."""

    def test_parts_without_modes_is_still_the_old_protocol(self) -> None:
        """⚠️ Our two lists come from separate diff frames, so this state is
        reachable on a real X2D between one push and the next. Keying the
        decision on the parts list instead would leave mode ``0``, miss the
        lookup, and report **auto** for every fan on the machine — the controls
        would blink out and back.
        """
        s = PrinterState()
        s.airduct_mode = 0
        s.airduct_parts = {10: {"type": 0, "state": 40, "range_start": 0, "range_end": 100}}

        assert airduct_mode_effective(s) == -1
        assert airduct_fan_control(s, 10) == FAN_CTRL

    def test_real_parts_are_not_discarded_in_that_window(self) -> None:
        """BS clears them and rebuilds 1/2/3; we keep them. Losing a real part
        id would take its control away and rename the fan generically."""
        s = PrinterState()
        s.airduct_parts = {10: {"type": 0, "state": 40, "range_start": 0, "range_end": 100}}

        assert set(airduct_parts_effective(s, "X2D")) == {10}

    def test_modes_present_means_the_reported_mode_counts(self) -> None:
        s = _state(mode=1, ctrl=[2], off=[10])

        assert airduct_mode_effective(s) == 1


class TestTheThreeValuesAreDistinct:
    def test_no_two_states_collide(self) -> None:
        """They travel to the frontend as strings; a duplicate would make two
        different situations render identically."""
        assert len({FAN_CTRL, FAN_OFF, FAN_AUTO}) == 3

    @pytest.mark.parametrize("value", [FAN_CTRL, FAN_OFF, FAN_AUTO])
    def test_they_are_plain_lowercase_strings(self, value: str) -> None:
        assert isinstance(value, str) and value == value.lower()
