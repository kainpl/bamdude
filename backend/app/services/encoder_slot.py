"""One video encoder at a time for the whole process, in the order it was asked for (G10 E, F).

The editor's encode and the layer timelapse's stitch are both an x264 run of
minutes. Spec r4 §12 gives them ONE encoder between them: two at once on a Pi
starve the prints the machine exists for.

The place is taken in one synchronous step — the check that the encoder is
free and the note that it is now ours have no await between them — so two
callers can never both find it free. Waiters are served first come, first
served; a waiter that is stopped or cancelled leaves the queue and holds
nothing; and whoever holds the encoder gives it back in a ``finally``.

Not a lock over the publication, the probe or the upload: only the encoder run.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_POLL = 0.1


class EncoderSlot:
    def __init__(self) -> None:
        self._owner: object | None = None
        self._queue: list[object] = []

    @property
    def busy(self) -> bool:
        return self._owner is not None

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
                if self._owner is None and self._queue[0] is ticket:
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

    def reset(self) -> None:
        """Lifespan startup only: a new process has no encoder running."""
        self._owner = None
        self._queue.clear()


encoder_slot = EncoderSlot()
