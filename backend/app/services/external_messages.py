"""Messages from external apps: what may be sent, who sent it, and how often (spec vault
60-specs/external-app-messages-spec, owner 2026-11-21).

Another program of the farm (Home Assistant, a script) tells the farm's people something through BamDude's
channels as the catalog event ``external_app_message``. This module holds the parts that are not the
notification pipeline's: the plain-text rule, the sender's name, and the per-key rate limit. The delivery
itself is ``NotificationService.on_external_app_message``; the door is ``POST /notifications/external-message``.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

EVENT = "external_app_message"
LEVELS = ("info", "warning", "error")
LEVEL_ICONS = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}

MAX_TITLE = 120
MAX_MESSAGE = 2000
MAX_LINK = 500

#: Messages one key may send in a minute; the 21st is refused with Retry-After.
PER_MINUTE = 20
_WINDOW_SECONDS = 60.0

# Every control character but the tab and the line feed. A carriage return goes too, so CRLF reads as LF.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def plain(text: str) -> str:
    """The text as plain text: no control characters, and no ``*`` -- the Telegram sender reads ``*`` in a
    rendered message as bold, so an app's asterisks would switch BamDude's formatting on and off."""
    return _CONTROL.sub("", text).replace("*", "∗").strip()


@dataclass(frozen=True)
class ExternalSender:
    """The key a message came in with, and its owner -- the one thing a message can never leave out."""

    api_key_id: int
    key_name: str
    owner_id: int
    owner_name: str

    @property
    def label(self) -> str:
        return f"{self.key_name} · {self.owner_name}"

    def as_extra(self) -> dict:
        return {
            "api_key_id": self.api_key_id,
            "key_name": self.key_name,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
        }


class _Window:
    """A sliding one-minute window per key, in this process's memory (a restart forgets it -- the limit is
    against a flood, not an accounting)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sent: dict[int, deque[float]] = {}

    def admit(self, key_id: int, now: float | None = None) -> int | None:
        """Take a place for one message: None when admitted, else the seconds to wait (at least 1)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            sent = self._sent.setdefault(key_id, deque())
            while sent and now - sent[0] >= _WINDOW_SECONDS:
                sent.popleft()
            if len(sent) >= PER_MINUTE:
                return max(1, math.ceil(_WINDOW_SECONDS - (now - sent[0])))
            sent.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._sent.clear()


_window = _Window()


def admit(key_id: int) -> int | None:
    return _window.admit(key_id)


def reset_rate_limit() -> None:
    """Forget every key's window (tests)."""
    _window.reset()
