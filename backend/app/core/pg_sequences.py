"""Raising a PostgreSQL id sequence above a bound, the one rule for m261 and the SQLite import (C047).

``setval`` is not transactional: once called it stays, even when the
transaction around it rolls back. So the sequence is only ever moved UP, and
only as far as the bound — a rollback then leaves a gap in the numbers, never
a number handed out twice.

The rule:

1. no bound — the sequence is left alone (``setval(0)`` is never needed);
2. exhaustion first: when the sequence has no next number left in
   ``[seqmin, seqmax]`` (it already handed out ``seqmax``), or no number
   strictly above the bound exists (the bound reaches ``seqmax``) — refused,
   naming the table. Asked BEFORE the no-op below: an exhausted sequence's
   computed "next" lies past ``seqmax`` and would otherwise read as already
   safe while the next INSERT fails (C047 acceptance A01);
3. the value the sequence hands out next is already above the bound — left
   alone (an explicit id 0 or ``seq = 0`` lands here: a plain SERIAL starts
   at 1);
4. otherwise ``setval(seq, bound, true)``: the next INSERT gets ``bound + 1``.

"Next" is ``last_value + 1`` once the sequence has been called, ``last_value``
before — true only for an increment of 1, which is therefore required.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text


class SequenceError(RuntimeError):
    """The sequence cannot be raised safely; the message names the table and why."""


@dataclass(frozen=True)
class SequenceState:
    name: str
    increment: int
    minimum: int
    maximum: int
    cache: int
    cycle: bool
    last_value: int
    is_called: bool

    @property
    def next_value(self) -> int:
        return self.last_value + 1 if self.is_called else self.last_value

    @property
    def issued(self) -> int:
        """The highest value handed out so far (``minimum - 1`` before the first call)."""
        return self.last_value if self.is_called else self.last_value - 1


async def sequence_state(conn, table: str) -> SequenceState | None:
    """The state of ``table``'s own ``id`` sequence, or None when ``id`` owns no sequence."""
    quoted = '"' + table.replace('"', '""') + '"'
    name = (await conn.execute(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": quoted})).scalar()
    if not name:
        return None
    increment, minimum, maximum, cache, cycle = (
        await conn.execute(
            text(
                "SELECT seqincrement, seqmin, seqmax, seqcache, seqcycle FROM pg_sequence"
                " WHERE seqrelid = to_regclass(:name)"
            ),
            {"name": name},
        )
    ).one()
    last_value, is_called = (await conn.execute(text(f"SELECT last_value, is_called FROM {name}"))).one()  # noqa: S608
    return SequenceState(name, increment, minimum, maximum, cache, cycle, last_value, is_called)


def target(state: SequenceState, bound: int | None, table: str) -> int | None:
    """The value to ``setval`` to, or None to leave the sequence as it is (the module's rule)."""
    if state.increment != 1:
        raise SequenceError(f"{table}: the id sequence has increment {state.increment}; only 1 is supported")
    if bound is None:
        return None
    if state.next_value > state.maximum or bound >= state.maximum:
        raise SequenceError(
            f"{table}: the id sequence is exhausted (bound {bound}, next {state.next_value}, maximum {state.maximum})"
        )
    if bound < state.next_value:
        return None
    return bound


async def raise_to(conn, table: str, bound: int | None, *, plain: bool, required: bool) -> int | None:
    """Apply the rule to ``table``'s id sequence. Returns the value set, or None when nothing moved.

    ``plain`` additionally requires ``CACHE 1`` and ``NO CYCLE``: a cached sequence
    can hand another session a number from its cache after ``setval`` (C047 r2
    probe). ``required`` refuses a table whose ``id`` owns no sequence.
    """
    state = await sequence_state(conn, table)
    if state is None:
        if required:
            raise SequenceError(f"{table}: id owns no sequence")
        return None
    if plain and state.cache != 1:
        raise SequenceError(f"{table}: the id sequence caches {state.cache} values; only CACHE 1 is supported")
    if plain and state.cycle:
        raise SequenceError(f"{table}: the id sequence is CYCLE; only NO CYCLE is supported")
    value = target(state, bound, table)
    if value is not None:
        await conn.execute(text("SELECT setval(:name, :value, true)"), {"name": state.name, "value": value})
    return value
