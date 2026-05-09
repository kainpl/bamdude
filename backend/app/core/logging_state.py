"""Runtime-mutable reference to the application log file handler.

The log handler is built at module-import time in ``main.py`` (before
the DB is available), but the ``log_retention_days`` setting is stored
in the DB and exposed via the Settings UI. This module bridges the gap:

- ``main.py`` calls :func:`set_app_log_handler` right after building the
  ``TimedRotatingFileHandler``.
- The lifespan startup hook queries the DB for ``log_retention_days``
  and calls :func:`update_log_retention` to apply the value.
- The settings update route calls :func:`update_log_retention` whenever
  the operator changes the value via the UI, so the new retention
  takes effect on the next rotation without restarting the backend.

Lives in its own module to dodge the circular import between
``main.py`` (creates the handler) and ``api/routes/settings.py``
(updates it on user action).
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger(__name__)

_handler: TimedRotatingFileHandler | None = None


def set_app_log_handler(handler: TimedRotatingFileHandler) -> None:
    """Stash the file-rotation handler so other modules can mutate it."""
    global _handler
    _handler = handler


def get_app_log_handler() -> TimedRotatingFileHandler | None:
    return _handler


def update_log_retention(days: int) -> None:
    """Update the live handler's ``backupCount`` so future rotations
    keep ``days`` historical files. Old archives beyond the new limit
    are purged on the next midnight rotation, not immediately —
    operators wanting an instant cleanup can manually delete files
    via the System page UI.
    """
    if _handler is None:
        return
    safe_days = max(1, int(days))
    _handler.backupCount = safe_days
    logger.info("Log retention updated: %d days", safe_days)


def app_log_filename_namer(default_name: str) -> str:
    """Rewrite the rotated log filename from the stdlib default
    ``bamdude.log.YYYY-MM-DD`` into the operator-friendly
    ``bamdude-YYYY-MM-DD.log`` shape.

    stdlib's ``TimedRotatingFileHandler`` produces files of the form
    ``<basename>.<suffix>``; with ``basename='bamdude.log'`` and our
    suffix ``%Y-%m-%d`` that lands as ``bamdude.log.2026-05-07``. The
    date-as-extension shape doesn't sort lexicographically alongside
    the live ``bamdude.log`` and breaks the ``*.log`` glob lots of log
    tooling assumes. Date-in-stem keeps the ``.log`` extension where
    operators (and tools like ``logrotate``) expect it.
    """
    from pathlib import Path

    p = Path(default_name)
    # The default name is built by joining basename + "." + suffix.
    # ``rpartition('.')`` splits on the last dot, separating the date
    # from "bamdude.log".
    base, sep, date = p.name.rpartition(".")
    if not sep:
        return default_name  # malformed — let stdlib handle it
    stem, sep2, ext = base.rpartition(".")
    if not sep2:
        return default_name  # basename had no extension — bail
    return str(p.parent / f"{stem}-{date}.{ext}")
