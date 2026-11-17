"""Short-lived copies of the camera frames that notifications link to.

Most channels take a frame as bytes. Home Assistant, Bark and a Slack-format
webhook fetch a URL themselves, and a template's ``{finish_photo_url}`` is a
link somebody opens from a chat — with no session either way. Those requests
are served a COPY kept here, never the archive's own photo: the archive route
is behind the media token like every other picture.

The file name is the credential. It carries 24 random bytes
(``secrets.token_urlsafe(24)``, 192 bits), the route serves only names of
exactly the shape written here, and a copy older than ``MAX_AGE_SECONDS`` is
refused even before a sweep removed it — so the age limit is the link's expiry.
A link opens this one picture and nothing else: no camera token, no archive.

The copies are derived and short-lived: not in backups, not in the database,
pruned by age whenever a new one is written.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from backend.app.core.config import settings
from backend.app.utils.safe_path import PathTraversalError, safe_join_under

logger = logging.getLogger(__name__)

# Three days (owner, 2026-11-17 -- as upstream): long enough for a link opened
# the next morning, short enough that an old chat is not a gallery.
MAX_AGE_SECONDS = 3 * 24 * 60 * 60

_TOKEN_BYTES = 24  # encodes to exactly 32 URL-safe base64 characters
_FILENAME_RE = re.compile(r"[a-z0-9_]+_\d{8}_\d{6}_[A-Za-z0-9_-]{32}\.jpg")

ROUTE_PREFIX = "/api/v1/notifications/photos/"


def notification_photos_dir() -> Path:
    return Path(settings.base_dir) / "notification-photos"


def _prune(directory: Path) -> None:
    """Remove copies past their age. Never the reason a notification fails."""
    try:
        cutoff = time.time() - MAX_AGE_SECONDS
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError as exc:
        logger.debug("Notification photos were not pruned: %s", exc)


def save_notification_photo(image_data: bytes, event_type: str) -> str:
    """Write a copy and return its file name. Blocking: call it in a thread."""
    directory = notification_photos_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _prune(directory)

    event = re.sub(r"[^a-z0-9_]", "", (event_type or "").lower()) or "event"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{event}_{stamp}_{secrets.token_urlsafe(_TOKEN_BYTES)}.jpg"
    (directory / filename).write_bytes(image_data)
    return filename


def find_notification_photo(filename: str) -> Path | None:
    """The copy of that name, or None unless it has the written shape, exists
    and is younger than ``MAX_AGE_SECONDS``."""
    if not _FILENAME_RE.fullmatch(filename or ""):
        return None
    try:
        candidate = safe_join_under(notification_photos_dir(), filename, http=False)
    except PathTraversalError:
        return None
    try:
        if not candidate.is_file() or candidate.stat().st_mtime < time.time() - MAX_AGE_SECONDS:
            return None
    except OSError:
        return None
    return candidate
