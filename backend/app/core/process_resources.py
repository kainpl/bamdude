"""Best-effort process limits and path-free descriptor diagnostics.

Imports are deliberately eager: diagnostics must not first load code at EMFILE.
The optional resource module is absent on Windows.
"""

import logging
import os
import sys
from collections import Counter

import psutil

try:
    import resource
except ImportError:
    resource = None

logger = logging.getLogger(__name__)
_FD_KIND_LIMIT = 4096


def raise_nofile_limit() -> None:
    """Raise only our soft limit, preserving high/unlimited values and hard."""
    if resource is None:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return
    infinity = resource.RLIM_INFINITY
    if soft == infinity or soft >= 65536:
        return
    target = 65536 if hard == infinity else min(65536, hard)
    if target <= soft:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return
    except (OSError, ValueError) as exc:
        logger.info("Could not raise descriptor soft limit (%s)", type(exc).__name__)
    if soft < 10240 and (hard == infinity or hard >= 10240):
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (10240, hard))
        except (OSError, ValueError) as exc:
            logger.info("Could not apply descriptor fallback (%s)", type(exc).__name__)


def _fd_kind(target: str) -> str:
    # readlink is procfs text; never stat the target (which could be a dead NAS).
    if target.startswith("socket:"):
        return "socket"
    if target.startswith("pipe:"):
        return "pipe"
    target = target.removesuffix(" (deleted)")
    if target.startswith("/") and not target.startswith("/dev/"):
        for suffix, kind in (("-wal", "wal_file"), ("-shm", "shm_file"), (".db", "db_file")):
            if target.endswith(suffix):
                return kind
        # Text alone cannot distinguish an ordinary file from a directory.
        return "other_regular"
    return "other"


def collect_fd_info() -> dict:
    """Current-process snapshots, not atomic and never containing paths/argv.

    Linux num_fds includes the temporary listing FD (usually +1); the kinds
    listing closes it before readlink, often yielding one `disappeared` entry.
    Concurrent FD reuse can change this. Neither is evidence of a leak.
    """
    out = {"count": None, "kinds": None, "soft_limit": None, "hard_limit": None, "kinds_truncated": False}
    if resource is not None:
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            out["soft_limit"] = -1 if soft == resource.RLIM_INFINITY else soft
            out["hard_limit"] = -1 if hard == resource.RLIM_INFINITY else hard
        except (OSError, ValueError):
            pass
    try:
        out["count"] = psutil.Process().num_fds()
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:
            entries = os.listdir("/proc/self/fd")  # listdir closes its own FD
        except OSError:
            return out
        out["kinds_truncated"] = len(entries) > _FD_KIND_LIMIT
        kinds = Counter()
        for entry in entries[:_FD_KIND_LIMIT]:
            try:
                kind = _fd_kind(os.readlink(f"/proc/self/fd/{entry}"))
            except OSError:
                kind = "disappeared"
            kinds[kind] += 1
        out["kinds"] = dict(kinds)
    return out
