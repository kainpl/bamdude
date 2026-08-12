"""One test per row of the transition table.

The deadband rule is the one worth guarding: applied on the way IN it would
make a threshold of 30 quietly mean 31, and no number on any screen would say
so.
"""

import pytest

from backend.app.services.sensor_alerts import ABOVE, BELOW, OK, next_state, template_for


def state(current, value, *, lo=None, hi=None, deadband=1.0):
    return next_state(current, value, min_value=lo, max_value=hi, deadband=deadband)


class TestEntering:
    def test_above_the_limit_raises(self):
        assert state(OK, 31.0, hi=30.0) == ABOVE

    def test_below_the_limit_raises(self):
        assert state(OK, 18.0, lo=20.0) == BELOW

    def test_exactly_on_the_limit_does_not_raise(self):
        # "not above 30" is satisfied by 30.
        assert state(OK, 30.0, hi=30.0) == OK

    def test_the_deadband_does_not_apply_on_the_way_in(self):
        # 30.5 is above 30 and must alarm at once. Applying the deadband here
        # would make the threshold on screen a lie.
        assert state(OK, 30.5, hi=30.0, deadband=1.0) == ABOVE


class TestLeaving:
    def test_it_holds_inside_the_deadband(self):
        # Back under the limit but not by enough: this is the flapping the
        # deadband exists to swallow.
        assert state(ABOVE, 29.5, hi=30.0, deadband=1.0) == ABOVE

    def test_it_clears_past_the_deadband(self):
        assert state(ABOVE, 29.0, hi=30.0, deadband=1.0) == OK

    def test_it_clears_from_below_past_the_deadband(self):
        assert state(BELOW, 21.0, lo=20.0, deadband=1.0) == OK

    def test_it_holds_inside_the_deadband_from_below(self):
        assert state(BELOW, 20.5, lo=20.0, deadband=1.0) == BELOW

    def test_a_zero_deadband_clears_at_the_limit(self):
        assert state(ABOVE, 30.0, hi=30.0, deadband=0.0) == OK


class TestCrossing:
    def test_it_can_go_straight_from_above_to_below(self):
        # Both limits set and the reading jumped past both.
        assert state(ABOVE, 10.0, lo=20.0, hi=30.0) == BELOW

    def test_it_can_go_straight_from_below_to_above(self):
        assert state(BELOW, 40.0, lo=20.0, hi=30.0) == ABOVE


class TestOneSidedLimits:
    def test_only_a_maximum_never_produces_below(self):
        assert state(OK, -5.0, hi=30.0) == OK

    def test_only_a_minimum_never_produces_above(self):
        assert state(OK, 500.0, lo=20.0) == OK


class TestWhichMessage:
    def test_no_change_says_nothing(self):
        assert template_for(ABOVE, ABOVE) is None
        assert template_for(OK, OK) is None

    @pytest.mark.parametrize(
        "previous,new,expected",
        [
            (OK, ABOVE, "sensor_above_max"),
            (OK, BELOW, "sensor_below_min"),
            (ABOVE, OK, "sensor_back_in_range"),
            (BELOW, OK, "sensor_back_in_range"),
            (ABOVE, BELOW, "sensor_below_min"),
            (BELOW, ABOVE, "sensor_above_max"),
        ],
    )
    def test_each_transition_names_its_own_sentence(self, previous, new, expected):
        assert template_for(previous, new) == expected
