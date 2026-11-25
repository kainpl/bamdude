"""C047 spec §B "raise rule", every branch, on the pure decision (no database).

Acceptance A01: a sequence that has already handed out its maximum computes a
"next" past ``seqmax``. Asked as "is next above the bound?" first, it passed as
already safe and the next INSERT failed; exhaustion must be asked first.
"""

import pytest

from backend.app.core.pg_sequences import SequenceError, SequenceState, target


def _state(last_value: int, is_called: bool, *, maximum: int = 2147483647, increment: int = 1) -> SequenceState:
    return SequenceState("public.spool_id_seq", increment, 1, maximum, 1, False, last_value, is_called)


@pytest.mark.parametrize(
    ("state", "bound", "expected"),
    [
        (_state(1, False), None, None),  # no bound: untouched
        (_state(1, False), 0, None),  # seq = 0 on a fresh SERIAL: it hands out 1 anyway
        (_state(5, True), 3, None),  # already past the bound
        (_state(5, True), 5, None),  # it hands out 6 next: already above a bound of 5
        (_state(5, True), 40, 40),
        (_state(1, False), 1, 1),
        (_state(199, True, maximum=200), 199, None),  # 200 is still free and already above the bound
        (_state(150, True, maximum=200), 199, 199),  # raised to the edge: the next INSERT gets seqmax itself
    ],
)
def test_the_rule_chooses_leave_or_raise(state, bound, expected):
    assert target(state, bound, "spool") == expected


@pytest.mark.parametrize(
    ("state", "bound"),
    [
        (_state(200, True, maximum=200), 200),  # A01: it already handed out its maximum
        (_state(200, True, maximum=200), 10),  # no next number at all, whatever the bound
        (_state(150, True, maximum=200), 200),  # nothing strictly above the bound exists
        (_state(1, False, maximum=200), 300),
    ],
)
def test_an_exhausted_sequence_is_refused_before_any_no_op(state, bound):
    with pytest.raises(SequenceError, match="spool: the id sequence is exhausted"):
        target(state, bound, "spool")


def test_only_an_increment_of_one_is_supported():
    with pytest.raises(SequenceError, match="increment 2"):
        target(_state(5, True, increment=2), 3, "spool")
