"""Shared path resolution helpers.

Centralises the DATA_DIR fallback used by ``auth.py`` (``.jwt_secret``) and
``encryption.py`` (``.mfa_encryption_key``) so both modules read the
environment variable fresh on every call. Reading fresh — instead of caching
the value at module import — is required so test fixtures can override
``DATA_DIR`` per-test via ``monkeypatch.setenv`` and have the override take
effect immediately.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_data_dir() -> Path:
    """Return the data directory, reading ``DATA_DIR`` fresh from env on each call.

    Falls back to ``<project_root>/data`` when ``DATA_DIR`` is not set, matching
    the behaviour of ``backend/app/core/auth.py:_get_jwt_secret``.
    """
    data_dir_env = os.environ.get("DATA_DIR")
    if data_dir_env:
        return Path(data_dir_env)
    return Path(__file__).parent.parent.parent.parent / "data"


def resolve_temp_dir() -> Path:
    """Where anything staging a file should stage it, ``TEMP_DIR`` read fresh.

    Defaults to ``<DATA_DIR>/tmp``: the system temp is the one filesystem an
    operator never sized for us, and the backup stages a COPY OF EVERYTHING
    there before it zips anything.
    """
    temp_dir_env = os.environ.get("TEMP_DIR")
    if temp_dir_env:
        return Path(temp_dir_env)
    return resolve_data_dir() / "tmp"


def _backup_roots() -> list[Path]:
    """The directories the backup copies wholesale, resolved."""
    from backend.app.core.config import settings
    from backend.app.services.backup_files import directories

    roots = []
    for path in directories(settings).values():
        try:
            roots.append(path.resolve())
        except OSError:  # An unreachable mount is somebody else's error to report.
            continue
    return roots


def install_process_temp_dir() -> Path:
    """Point every ``tempfile`` user at the scratch directory, once, at startup.

    ⚠️ Scratch may not live INSIDE a directory the backup copies. `copy_tree`
    inventories a tree, copies it, then re-inventories and refuses if it
    changed — so a staging directory growing inside the very tree being copied
    would fail every backup, and on the passes where it did not it would copy
    the backup into itself. A configured path that lands there is ignored with
    an error rather than honoured.
    """
    chosen = resolve_temp_dir()
    fallback = resolve_data_dir() / "tmp"
    try:
        resolved = chosen.resolve()
        inside = next((r for r in _backup_roots() if resolved == r or r in resolved.parents), None)
    except OSError as exc:
        logger.error("TEMP_DIR %s cannot be resolved (%s) — using %s", chosen, exc, fallback)
        chosen, inside = fallback, None
    if inside is not None:
        logger.error(
            "TEMP_DIR %s is inside %s, which the backup copies whole — using %s instead",
            chosen,
            inside,
            fallback,
        )
        chosen = fallback

    chosen.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(chosen)
    logger.info("Scratch directory: %s", chosen)
    return chosen
