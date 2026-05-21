"""Anonymous per-install identifier for opt-out telemetry.

A random UUID generated once at first boot and stored in ``DATA_DIR/.install_id``
(mode 0600), mirroring the secret-file pattern in ``core/encryption.py`` /
``core/auth.py``. It is NOT linked to any user, email, IP or hardware id — it
only lets the telemetry backend collapse a given install's daily snapshots into
one row. Returns ``None`` when the data dir isn't writable, in which case
telemetry simply no-ops.
"""

import logging
import os
import uuid
from pathlib import Path

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

_FILE_NAME = ".install_id"
_cached: str | None = None


def _path() -> Path:
    return Path(settings.data_dir) / _FILE_NAME


def _read() -> str | None:
    try:
        path = _path()
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except OSError as e:
        logger.debug("install_id read failed: %s", e)
    return None


def get_install_id() -> str | None:
    """Return the install id, creating it once on first call."""
    global _cached
    if _cached:
        return _cached

    existing = _read()
    if existing:
        _cached = existing
        return _cached

    new_id = str(uuid.uuid4())
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL so two workers racing to create it can't clobber each other;
        # 0o600 from the start so it's never world-readable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, new_id.encode("utf-8"))
        finally:
            os.close(fd)
        _cached = new_id
        return _cached
    except FileExistsError:
        # Lost the race — read back whatever the winner wrote.
        existing = _read()
        if existing:
            _cached = existing
            return _cached
    except OSError as e:
        logger.debug("install_id create failed: %s", e)
    return None


def reset_install_id() -> str | None:
    """Forget the current id and mint a fresh one (used by opt-out / reset)."""
    global _cached
    _cached = None
    try:
        _path().unlink(missing_ok=True)
    except OSError as e:
        logger.debug("install_id reset failed: %s", e)
    return get_install_id()
