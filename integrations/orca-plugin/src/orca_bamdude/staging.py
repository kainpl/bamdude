"""The plugin's own copy of the file it sends (spec r2 §6).

The path the host hands over is not immutable: the slicer may rewrite it, the user may save over it. So the bytes
are copied once into plugin-owned staging, hashed as they are copied, and only that copy is inspected and
uploaded. The source must be a regular file within the size cap, and must not change while it is copied.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass

CHUNK = 1024 * 1024


class StagingError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Staged:
    path: str
    sha256: str
    size: int
    filename: str


def _identity(info: os.stat_result) -> tuple:
    return info.st_size, info.st_mtime_ns


def stage(
    source: str,
    staging_dir: str,
    max_bytes: int,
    *,
    filename: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Staged:
    """Copy ``source`` into ``staging_dir`` as ``<sha256>.3mf`` and return it; the partial copy never survives."""
    try:
        before = os.stat(source)
    except OSError as exc:
        raise StagingError("source_missing") from exc
    if not stat.S_ISREG(before.st_mode):
        raise StagingError("not_a_file")
    if before.st_size > max_bytes:
        raise StagingError("too_large")
    if before.st_size == 0:
        raise StagingError("empty")
    os.makedirs(staging_dir, exist_ok=True)
    partial = os.path.join(staging_dir, f".{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(source, "rb") as src, open(partial, "wb") as dst:
            opened = os.fstat(src.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                raise StagingError("source_changed")
            while True:
                if cancelled is not None and cancelled():
                    raise StagingError("cancelled")
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise StagingError("too_large")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if _identity(os.stat(source)) != _identity(before) or size != before.st_size:
            raise StagingError("source_changed")
        sha = digest.hexdigest()
        final = os.path.join(staging_dir, f"{sha}.3mf")
        os.replace(partial, final)
    except StagingError:
        _remove(partial)
        raise
    except OSError as exc:
        _remove(partial)
        raise StagingError("disk_error") from exc
    return Staged(path=final, sha256=sha, size=size, filename=filename or os.path.basename(source))


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def discard(staged: Staged) -> None:
    _remove(staged.path)
