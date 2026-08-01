"""A log line must never end at the colon.

Found by reading a support log during a physical disconnect test:

    Zigbee: read of 0x0000 failed, waiting for a report instead:

Nothing after it. The read had timed out — which is what happens every time a
device is unplugged, sleeps, or moves out of range — and
``str(TimeoutError())`` is the empty string. So the one exception that
matters most on this path is the one that says nothing at all.

The coordinator already carried a fallback for exactly this class of problem,
measured against a closed port in phase 4. It just was not shared, so nine other
call sites across the package had no protection. These tests moved here with the
helper.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.zigbee.errors import describe_exception, is_closed_loop_artefact
from backend.app.services.zigbee.reporting import _read_into_cache


class TestTheReasonIsNeverEmpty:
    """`reason` is the whole explanation, so it must always say something.

    Measured on hardware: pointing the coordinator at a closed port produced
    `state: error` with `reason: ""`, because the exception bellows raised
    stringifies to nothing. Every consumer built in phase 4 — the settings card,
    the status badge, the toast — falls back to a generic label in that case, so
    the operator is told "the radio is down" and nothing about why.

    An exception class name is a poor explanation. It is still infinitely better
    than an empty string.
    """

    def test_an_exception_with_no_message_still_yields_a_reason(self):
        assert describe_exception(OSError()) == "OSError"

    def test_a_message_is_preferred_when_there_is_one(self):
        assert describe_exception(OSError("no such device")) == "no such device"

    def test_whitespace_counts_as_empty(self):
        assert describe_exception(OSError("   ")) == "OSError"

    def test_none_is_described_rather_than_printed(self):
        """`connection_lost(None)` is what bellows actually passed on a dropped
        socket, and "Connection to the Zigbee radio was lost: None" is not an
        explanation."""
        assert describe_exception(None) == "the connection closed without an error"

    @pytest.mark.parametrize("exc", [TimeoutError(), ValueError(""), ValueError("  "), RuntimeError(), OSError()])
    def test_nothing_that_stringifies_to_nothing_escapes(self, exc):
        """The property the log line actually needs, stated for every
        empty-message exception rather than for the one we happened to hit."""
        assert describe_exception(exc).strip()


class TestATimeoutIsTheOrdinaryFailure:
    """The case that produced the bare colon, and the one operators read most.

    Every attribute read against a device that has been unplugged, gone out of
    range, or is simply asleep ends in a timeout. "TimeoutError" would be
    accurate and would tell them nothing.

    Spelled as the builtin throughout: ``asyncio.TimeoutError`` has been the same
    object since 3.11, so the two names in the zigpy stack are one exception.
    """

    def test_a_timeout_is_named_by_what_happened(self):
        assert describe_exception(TimeoutError()) == "the device did not answer in time"

    def test_the_artefact_still_wins_when_a_timeout_wraps_it(self):
        """Order matters: a real cause buried under a timeout must not be
        flattened into "did not answer"."""
        try:
            try:
                raise TypeError("object NoneType can't be used in 'await' expression")
            except TypeError as inner:
                raise TimeoutError() from inner
        except TimeoutError as exc:
            assert "radio" in describe_exception(exc)


class TestClosedLoopArtefact:
    """bellows' ``await None`` must never reach the operator as the reason.

    ``ThreadsafeProxy`` hands back ``None`` instead of a coroutine once its
    thread's loop is closed, so every ``await`` on the Gateway dies with a
    TypeError that names nothing. The loop is closed by ``uart.connect``'s
    ``connection_done`` callback, i.e. precisely when the link to the radio
    ended — and it masks twice: ``EZSP._startup_reset`` hits it, its handler
    calls ``disconnect()``, which hits it again, and the second one escapes.

    Reproduced against a TCP server that accepts and then resets: before the
    fix the coordinator reported "object NoneType can't be used in 'await'
    expression" and the interpreter would not exit.
    """

    def test_the_artefact_is_translated(self):
        reason = describe_exception(TypeError("object NoneType can't be used in 'await' expression"))

        assert "NoneType" not in reason
        assert "radio" in reason

    def test_it_is_found_through_the_exception_chain(self):
        """The escaping exception is the SECOND one raised, so the marker is
        usually not the outermost."""
        try:
            try:
                raise TypeError("object NoneType can't be used in 'await' expression")
            except TypeError as inner:
                raise RuntimeError("cleanup failed") from inner
        except RuntimeError as outer:
            assert "NoneType" not in describe_exception(outer)

    def test_a_real_cause_is_left_alone(self):
        """Only the artefact is rewritten. A genuine failure keeps its own
        words — measured: a refused connect still reads TransientConnectionError."""
        assert describe_exception(OSError("no such device")) == "no such device"
        assert describe_exception(ConnectionRefusedError()) == "ConnectionRefusedError"

    def test_an_unrelated_typeerror_is_left_alone(self):
        assert describe_exception(TypeError("unsupported operand type(s)")) == "unsupported operand type(s)"

    def test_a_self_referential_chain_terminates(self):
        """``__context__`` can point back at an exception already seen."""
        a = ValueError("a")
        b = ValueError("b")
        a.__context__ = b
        b.__context__ = a

        assert is_closed_loop_artefact(a) is False


class TestTheLogLineThatStartedThis:
    @pytest.mark.asyncio
    async def test_a_timed_out_read_logs_a_reason_after_the_colon(self, caplog):
        cluster = MagicMock()
        cluster.read_attributes = AsyncMock(side_effect=TimeoutError())

        with caplog.at_level("INFO"):
            cached = await _read_into_cache(cluster, MagicMock(), 0x0000)

        assert cached is False
        line = next(r.getMessage() for r in caplog.records if "waiting for a report instead" in r.getMessage())
        assert not line.rstrip().endswith(":"), f"the reason went missing again: {line!r}"
        assert line.endswith("the device did not answer in time")

    @pytest.mark.asyncio
    async def test_a_failed_read_never_caches_and_never_raises(self, caplog):
        """The reason it is swallowed at all: an absent device is not an error,
        it only means this round has no fresh value."""
        cluster = MagicMock()
        cluster.read_attributes = AsyncMock(side_effect=TimeoutError())
        listener = MagicMock()

        with caplog.at_level("INFO"):
            assert await _read_into_cache(cluster, listener, 0x0000) is False

        listener.attribute_updated.assert_not_called()
        cluster.get.assert_not_called()
