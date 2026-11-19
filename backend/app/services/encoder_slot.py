"""One video encoder at a time for the whole process, in the order it was asked for (G10 E, F).

The editor's encode and the layer timelapse's stitch are both an x264 run of
minutes. Spec r4 §12 gives them ONE encoder between them: two at once on a Pi
starve the prints the machine exists for.

The place is taken in one synchronous step — the check that the encoder is
free and the note that it is now ours have no await between them — so two
callers can never both find it free. Waiters are served first come, first
served; a waiter that is stopped or cancelled leaves the queue and holds
nothing; and whoever holds the encoder gives it back in a ``finally``.

⚠️ **The encoder is given back when the PROCESS has ended, not when its caller
has.** A child whose end ``media_child`` did not see (``unreaped``) may still be
encoding: its holder hands the place to :meth:`EncoderSlot.keep_until_reaped`
with the child's own end event, and the place stays taken until that event is
set — for the life of the process when there is no event to wait for (Codex
E2-R / F3-R).

Not a lock over the publication, the probe or the upload: only the encoder run.
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

_POLL = 0.1


class _Reaping:
    """The holder of a place whose child was not seen to end: no caller's ticket can give it back."""


class EncoderSlot:
    def __init__(self) -> None:
        self._owner: object | None = None
        #: Set when the owner is a :class:`_Reaping` hold that ends with its child; ``None`` = never.
        self._owner_end: threading.Event | None = None
        self._queue: list[object] = []

    def _free(self) -> bool:
        """Is the encoder free now? A hold that waits for a child ends here, once that child has ended."""
        if self._owner is None:
            return True
        if isinstance(self._owner, _Reaping) and self._owner_end is not None and self._owner_end.is_set():
            logger.info("[ENCODER] an encoder whose end was not seen at once has ended; its place is free again")
            self._owner = None
            self._owner_end = None
            return True
        return False

    @property
    def busy(self) -> bool:
        return not self._free()

    def waiting(self) -> int:
        return len(self._queue)

    async def acquire(self, stop: asyncio.Event | None = None) -> object | None:
        """Wait for the encoder. Returns the ticket to give back, or ``None`` when ``stop`` was set first."""
        ticket = object()
        self._queue.append(ticket)
        try:
            while True:
                if stop is not None and stop.is_set():
                    return None
                # One synchronous step: free, and first in line — then it is ours.
                if self._queue[0] is ticket and self._free():
                    self._owner = ticket
                    return ticket
                await asyncio.sleep(_POLL)
        finally:
            # Taken, stopped or cancelled: this caller no longer waits.
            if ticket in self._queue:
                self._queue.remove(ticket)

    def release(self, ticket: object | None) -> None:
        """Give the encoder back. A ticket that does not hold it changes nothing."""
        if ticket is not None and self._owner is ticket:
            self._owner = None
            self._owner_end = None

    def keep_until_reaped(self, ticket: object | None, ended: threading.Event | None) -> None:
        """The holder's child was not seen to end: the place stays taken until ``ended`` is set.

        ``ended`` is the child's own end (``media_child.ChildResult.ended``);
        ``None`` keeps the place for the life of the process. The ticket stops
        being the holder, so a later :meth:`release` of it changes nothing.
        """
        if ticket is None or self._owner is not ticket:
            return
        self._owner = _Reaping()
        self._owner_end = ended
        logger.warning("[ENCODER] the encoder's end was not seen; no other encoder starts until it has ended")

    def reset(self) -> None:
        """Lifespan startup only: a new process has no encoder running."""
        self._owner = None
        self._owner_end = None
        self._queue.clear()


encoder_slot = EncoderSlot()
