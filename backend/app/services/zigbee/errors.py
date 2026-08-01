"""Turning an exception into something an operator can act on.

Shared because the zigbee stack raises a lot of exceptions that stringify to
nothing, and every one of them used to reach a log line or a status field as an
empty space after a colon.
"""

from __future__ import annotations


def describe_exception(exc: BaseException | None) -> str:
    """A reason that always says something.

    ``reason`` is the whole explanation of why the radio is not up — the settings
    card renders it verbatim, the status badge falls back to a generic label
    without it, and the toast has nothing else to show. So an empty one defeats
    every consumer at once.

    Measured on hardware: pointing the coordinator at a closed port produced
    ``state: error`` with ``reason: ""``, because what bellows raised stringifies
    to nothing. And ``connection_lost`` was called with ``None``, which rendered
    as "Connection to the Zigbee radio was lost: None".

    An exception class name is a poor explanation. It is still far better than a
    blank, which reads as "no idea, and we are not saying".
    """
    if exc is None:
        return "the connection closed without an error"
    if is_closed_loop_artefact(exc):
        return _CLOSED_LOOP_REASON
    if isinstance(exc, TimeoutError):
        return _TIMEOUT_REASON
    message = str(exc).strip()
    return message or type(exc).__name__


# A timeout is the ordinary way a Zigbee device fails, and the one exception here
# that carries no message at all — ``str(asyncio.TimeoutError())`` is "". Every
# attribute read against a device that has been unplugged, has gone out of range
# or is simply asleep ends here, so this is the single most common thing an
# operator reads in the log. "TimeoutError" is accurate and tells them nothing;
# name the situation instead.
_TIMEOUT_REASON = "the device did not answer in time"


# What bellows raises once its I/O thread's loop is gone, and what to say instead.
#
# ``bellows.thread.ThreadsafeProxy`` wraps the Gateway so calls hop onto the
# reader thread's loop. When that loop is closed it logs "Attempted to use a
# closed event loop" and takes a bare ``return`` — handing back ``None`` where
# the caller expects a coroutine. Every ``await`` on it then dies with this
# TypeError.
#
# The loop is closed by ``bellows.uart.connect``, which registers
# ``connection_done.add_done_callback(lambda _: thread.force_stop())``. So this
# fires precisely when the link to the radio ENDED — dropped mid-startup, or
# never completed — and the message describes none of that.
#
# Worse, it masks twice. Reproduced against a server that accepts then resets:
# ``EZSP._startup_reset`` awaits the proxy and gets this TypeError; its own
# handler calls ``disconnect()``, which awaits the proxy again and raises the
# SAME TypeError; that second one is what reaches us. The real cause is gone
# before it ever had a name, which is why the operator saw
# "object NoneType can't be used in 'await' expression" for a dongle that had
# simply gone away.
#
# Matched on the message rather than the type: a bare ``TypeError`` from the
# radio stack could be anything, but this exact sentence is CPython's wording
# for awaiting None, and inside a bellows failure it has only this one source.
_AWAIT_NONE_MESSAGE = "object NoneType can't be used in 'await' expression"
_CLOSED_LOOP_REASON = (
    "the connection to the radio ended during startup (the radio was unplugged, reset, or taken by another program)"
)


def is_closed_loop_artefact(exc: BaseException) -> bool:
    """True when this is bellows' closed-loop artefact rather than a real cause.

    Walks the ``__cause__``/``__context__`` chain: the escaping exception is the
    second one raised, so the marker can sit at any depth.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TypeError) and _AWAIT_NONE_MESSAGE in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False
