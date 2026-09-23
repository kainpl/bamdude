"""Small process-tree ownership helpers shared by local worker supervisors."""

from __future__ import annotations

import os
import signal

import psutil


def descendants(pid: int) -> list[psutil.Process]:
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []


def kill_owned_group(pid: int) -> None:
    """Kill only a process group created by our own start_new_session spawn."""
    if os.name == "nt":
        raise RuntimeError("POSIX process groups are unavailable on Windows")
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def descendants_reaped(children: list[psutil.Process], *, timeout: float = 5) -> bool:
    _, alive = psutil.wait_procs(children, timeout=timeout)
    return not alive
