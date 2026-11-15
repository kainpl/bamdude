"""What both download-to-file transports share about their local file (G10 A, review A2/A5).

Two rules, one place:

* **The partial file belongs to the transfer until it is gone.** Every way out
  but a completed, checked file removes it — a budget, a reserve, a refusal, a
  dead socket, a cancel and an error nobody expected. A file that cannot be
  removed is not a quiet leftover: :func:`discard_staging` raises
  :class:`StagingNotRemoved`, because "on anything but ``completed`` the
  destination does not exist" is what a :class:`TransferOutcome` promises.
* **The volume's reserve is checked before the first write**, every
  :data:`RESERVE_CHECK_EVERY` bytes after it, and once more for the unchecked
  tail — never once per chunk, and never skipped for a short file. The schedule
  decides; the transport asks the disk (the FTP worker on its own thread, the
  tunnel off the event loop).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: How often a streamed download re-reads the destination volume's free space.
RESERVE_CHECK_EVERY = 8 * 1024 * 1024


class StagingNotRemoved(RuntimeError):
    """A transfer that did not complete could not remove its partial file.

    Raised instead of an outcome: an outcome would promise that the file is gone.
    """


def discard_staging(path: Path) -> None:
    """Remove a transfer's partial file, or raise :class:`StagingNotRemoved` when it stays."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "A printer file transfer could not remove its partial file %s (%s)", path.name, type(exc).__name__
        )
        raise StagingNotRemoved(f"partial file {path.name} was not removed") from exc


class ReserveSchedule:
    """When a streamed download asks the disk how much room is left.

    ``due_before(received)`` is asked before each chunk is written, with the
    count that includes it: True for the first chunk and whenever
    :data:`RESERVE_CHECK_EVERY` bytes have arrived since the last check, which
    then covers this chunk too. ``due_final(received)`` is asked once the file
    is written: True when bytes arrived after the last check (a file of one
    chunk has none; an empty file has never been checked). ``reserve == 0``
    checks nothing.
    """

    def __init__(self, reserve: int, every: int = RESERVE_CHECK_EVERY):
        self.reserve = max(0, int(reserve or 0))
        self.every = every
        self._checked_at: int | None = None

    def due_before(self, received: int) -> bool:
        if not self.reserve:
            return False
        if self._checked_at is None or received - self._checked_at >= self.every:
            self._checked_at = received
            return True
        return False

    def due_final(self, received: int) -> bool:
        return bool(self.reserve) and (self._checked_at is None or received > self._checked_at)

    def fits(self, free: int, incoming: int = 0) -> bool:
        """Whether ``incoming`` more bytes still leave the reserve free."""
        return free - incoming >= self.reserve
