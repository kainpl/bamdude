"""Advisory PID lock so two BamDude processes do not open one radio.

Advisory on purpose, and the limit matters enough to state plainly: this cannot
stop Zigbee2MQTT or Home Assistant — on this machine or another one — from
opening the same ``socket://``. It catches the accident that actually happens,
which is uvicorn ``--reload`` running two workers, or a second instance started
by hand. When the stream misbehaves anyway, the coordinator's error text names
the external-holder possibility, because that is the least guessable cause.

A stale lock is reclaimed rather than honoured. Refusing on a dead PID would
mean an unclean shutdown bricks Zigbee until someone deletes a file they do not
know exists — a worse failure than the one being prevented, and one with no
visible cause.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Paths held by a live RadioLock in THIS process. The lock file alone cannot
# answer at this scope: its PID is ours either way, so a second instance in the
# same process would read its own PID and treat a live lock as its own stale
# leftover. Both readings are needed — "another process holds it" comes from the
# file, "another object here holds it" comes from this set.
_held_in_process: set[Path] = set()


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is a live process.

    ``os.kill(pid, 0)`` delivers no signal — it is the existence check. On
    Windows a dead PID surfaces as a plain ``OSError`` rather than
    ``ProcessLookupError``, which is why the broad branch is here and treats it
    as dead: erring toward "reclaimable" keeps a crashed instance from locking
    the feature out forever.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


class RadioLock:
    """Owns ``path`` for the lifetime of this process's coordinator."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._held = False

    def acquire(self) -> bool:
        """True when this process may open the radio."""
        resolved = self.path.absolute()
        if resolved in _held_in_process:
            logger.warning("Zigbee radio is already held in this process (lock: %s)", self.path)
            return False

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create Zigbee lock directory %s: %s", self.path.parent, exc)
            return False

        if self.path.exists():
            owner: int | None
            try:
                owner = int(self.path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                # Unreadable, empty, or half-written by a crash: treat as stale.
                owner = None

            if owner is not None and owner != os.getpid() and _pid_alive(owner):
                logger.warning("Zigbee radio is already held by PID %s (lock: %s)", owner, self.path)
                return False
            logger.info("Reclaiming stale Zigbee radio lock at %s", self.path)

        try:
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write Zigbee radio lock %s: %s", self.path, exc)
            return False

        _held_in_process.add(resolved)
        self._held = True
        return True

    def release(self) -> None:
        """No-op unless this instance actually holds the lock.

        The guard is not defensive padding: without it, an instance that was
        refused the lock would delete the file belonging to the process that
        legitimately holds the radio.
        """
        if not self._held:
            return
        _held_in_process.discard(self.path.absolute())
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove Zigbee radio lock %s: %s", self.path, exc)
        self._held = False
