"""Narrow, ownership-neutral filesystem helpers for local worker staging."""

from __future__ import annotations

import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CleanupResult:
    status: str
    path: Path
    error_type: str | None = None
    error_code: int | None = None


def _info(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        raise ValueError("reparse_point")
    return info


def _entries(path: Path) -> list[Path]:
    before = _info(path)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("not_directory")
    entries = list(path.iterdir())
    after = _info(path)
    if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
        raise ValueError("changed_during_scan")
    return entries


def cleanup_owned(path: Path, *, deadline: float | None = None) -> CleanupResult:
    """Delete only a caller-proven owned root; a failed syscall remains joined."""
    deadline = deadline if deadline is not None else time.monotonic() + 2.0
    last: OSError | ValueError | None = None
    for attempt in range(3):
        try:
            info = _info(path)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("not_directory")
            shutil.rmtree(path)
            return CleanupResult("removed", path)
        except FileNotFoundError:
            return CleanupResult("absent", path)
        except (OSError, ValueError) as exc:
            last = exc
        delay = (0.1, 0.3)[attempt] if attempt < 2 else 0
        if not delay or time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
    assert last is not None
    return CleanupResult(
        "retained_error", path, type(last).__name__, getattr(last, "winerror", None) or getattr(last, "errno", None)
    )


def retained_entries(staging: Path, current: Path, runtime: str) -> list[tuple[Path, str, str]]:
    """Shallow, read-only classification; never inspect payload trees or follow links."""
    findings = []
    try:
        entries = _entries(staging)
    except FileNotFoundError:
        return findings
    except (OSError, ValueError) as exc:
        return [(staging, "unknown", type(exc).__name__)]
    for path in entries:
        if path == current:
            continue
        try:
            if len(path.name) != 32 or any(c not in "0123456789abcdef" for c in path.name):
                findings.append((path, "unknown", "unexpected_name"))
                continue
            if not stat.S_ISDIR(_info(path).st_mode):
                findings.append((path, "unknown", "not_directory"))
                continue
            outcome = _classify_generation(path, runtime)
            findings.append((path, outcome, "known_skeleton" if outcome == "empty_skeleton" else "retained_entries"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            findings.append((path, "unknown", type(exc).__name__))
    return findings


def _empty_directory(path: Path) -> bool:
    return not _entries(path)


def _classify_generation(root: Path, runtime: str) -> str:
    entries = {entry.name: entry for entry in _entries(root)}
    if set(entries) - {"main", "service"}:
        for entry in entries.values():
            _info(entry)
        return "retained_content"
    if "main" in entries and not _empty_directory(entries["main"]):
        return "retained_content"
    if "service" not in entries:
        return "empty_skeleton"
    service = entries["service"]
    if runtime == "preview":
        return "empty_skeleton" if _empty_directory(service) else "retained_content"
    service_entries = {entry.name: entry for entry in _entries(service)}
    if set(service_entries) - {"cache", "child.ready"}:
        for entry in service_entries.values():
            _info(entry)
        return "retained_content"
    if "cache" in service_entries and not _empty_directory(service_entries["cache"]):
        return "retained_content"
    if "child.ready" in service_entries:
        marker = _info(service_entries["child.ready"])
        if not stat.S_ISREG(marker.st_mode):
            raise ValueError("not_regular_file")
        if not 1 <= marker.st_size <= 32:
            return "retained_content"
    return "empty_skeleton"


def abandoned_attempts(service_root: Path) -> tuple[list[Path], list[Path]]:
    """List direct attempt directories only after the caller has reaped that service."""
    attempts, unknown = [], []
    try:
        entries = _entries(service_root)
    except FileNotFoundError:
        return attempts, unknown
    except (OSError, ValueError):
        return attempts, [service_root]
    for path in entries:
        if path.name in {"cache", "child.ready"}:
            continue
        try:
            info = _info(path)
            if len(path.name) == 32 and all(c in "0123456789abcdef" for c in path.name) and stat.S_ISDIR(info.st_mode):
                attempts.append(path)
            else:
                unknown.append(path)
        except (OSError, ValueError):
            unknown.append(path)
    return attempts, unknown
