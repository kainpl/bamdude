"""The processes of one part-render attempt, recorded by their parents and proven gone (plan E3, R12).

The attempt is a chain -- worker, guardian, part_render, Node -- and every link is written by its PARENT.
``<name>.launch`` comes BEFORE the spawn, ``<name>.pid`` (pid + create_time) BEFORE the new process gets
any input. So a process without a record has done nothing yet: it waits on a pipe its parent holds. A
launch without a record is not proof of absence (consilium E3-R1).

``tree_gone`` is the proof the worker AND main ask before an attempt counts as over. A recorded process
that still runs is killed and waited for; one that cannot be shown dead fails the proof. The records
also see a process orphaned when its parent died, which ``descendants_reaped`` cannot. ``create_time``
tells a recycled pid from ours, so the proof never kills a stranger.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psutil

RECORDS = ("guardian", "child", "node")
_SAME_PROCESS_SECONDS = 0.01


def launch(attempt_dir: Path, name: str) -> None:
    """Written by a parent before it spawns ``name``: from here on a missing record is unknown, not absent."""
    with (attempt_dir / f"{name}.launch").open("a"):
        pass


def record(path: Path, pid: int) -> None:
    """Write the record atomically: a reader sees the whole record or none."""
    created = psutil.Process(pid).create_time()
    part = path.with_name(path.name + ".part")
    with part.open("x", encoding="ascii") as out:
        json.dump({"pid": pid, "create_time": created}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(part, path)


def _gone(entry: Path, timeout: float) -> bool:
    try:
        data = json.loads(entry.read_text(encoding="ascii"))
        pid, created = int(data["pid"]), float(data["create_time"])
    except (OSError, ValueError, KeyError, TypeError):
        return False  # a record that cannot be read proves nothing
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) > _SAME_PROCESS_SECONDS:
            return True  # the pid is someone else's now: ours is gone
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        process.kill()
        process.wait(timeout=timeout)
        return True
    except psutil.NoSuchProcess:
        return True
    except (psutil.TimeoutExpired, psutil.AccessDenied):
        return False


def tree_gone(attempt_dir: Path, *, strict: bool, timeout: float = 5) -> bool:
    """Every process of the attempt is gone.

    Every record must be proven dead. ``strict`` is main's proof after the worker itself died: a launch
    without its record -- or a half-written one -- may have produced a process outside every tree main can
    see, so it fails the proof and closes admission. Without ``strict``, as the worker asks right after
    ``PreviewProcess.stop()`` killed the attempt's group / Job Object, an unrecorded process was inside
    that group and went with it.
    """
    for name in RECORDS:
        pid_file = attempt_dir / f"{name}.pid"
        if pid_file.exists():
            if not _gone(pid_file, timeout):
                return False
        elif strict and ((attempt_dir / f"{name}.launch").exists() or (attempt_dir / f"{name}.pid.part").exists()):
            return False
    return True
