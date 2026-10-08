"""The processes of one part-render attempt, recorded by their parents and proven gone (plan E3, R12).

The attempt is a chain -- worker, guardian, part_render, Node -- and every link is written by its PARENT.
``<name>.launch`` comes BEFORE the spawn, ``<name>.pid`` (pid + create_time) BEFORE the new process gets
any input. So a process without a record has done nothing yet: it waits on a pipe its parent holds. A
launch without a record is not proof of absence (consilium E3-R1).

``tree_gone`` is the proof the worker AND main ask before an attempt counts as over. A recorded process
that still runs is killed and waited for; one that cannot be shown dead fails the proof. The records
also see a process orphaned when its parent died, which ``descendants_reaped`` cannot. ``create_time``
tells a recycled pid from ours, so the proof never kills a stranger.

A generation of main records itself (``owner``), its worker guardian (``worker``, by main) and the worker
(``service``, by that guardian) the same way, so ``generation_gone`` can prove an EARLIER run over after
main itself died -- by records, parents first, never by a scan of the process table (consilium E3-I-R1).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil

RECORDS = ("guardian", "child", "node")  # an attempt's, parents first
GENERATION_RECORDS = ("worker", "service")  # a generation's own, beside owner.pid; parents first
# who wrote a record -- whose death closes the pipe a launched but unrecorded process waits on
_PARENT = {"worker": "owner", "service": "worker", "guardian": "service", "child": "guardian", "node": "child"}
_SAME_PROCESS_SECONDS = 0.01
# Linux derives create_time from the boot time, and the boot time moves when the wall clock is stepped (an
# RTC-less Pi syncing NTP after start): a record keeps the distance from boot there (final review M2).
_BOOT_RELATIVE = sys.platform.startswith("linux")


def _started(process: psutil.Process) -> float:
    """When ``process`` started, comparable across processes and across a clock step."""
    if _BOOT_RELATIVE:
        return process.create_time() - psutil.boot_time()
    return process.create_time()


def launch(attempt_dir: Path, name: str) -> None:
    """Written by a parent before it spawns ``name``: from here on a missing record is unknown, not absent."""
    with (attempt_dir / f"{name}.launch").open("a"):
        pass


def record(path: Path, pid: int) -> None:
    """Write the record atomically: a reader sees the whole record or none."""
    created = _started(psutil.Process(pid))
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="ascii") as out:  # a .part left by a write that failed is no record
        json.dump({"pid": pid, "create_time": created}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(part, path)


def _gone(entry: Path, timeout: float, *, kill: bool = True) -> bool:
    """The recorded process has ended -- killed first when ``kill`` and it still runs. An exited process this
    process cannot reap (an orphan whose new parent never reaps: a container's PID 1 without a reaper) stays
    a zombie, and a zombie has ended (consilium E3-I-R3)."""
    try:
        data = json.loads(entry.read_text(encoding="ascii"))
        pid, created = int(data["pid"]), float(data["create_time"])
    except (OSError, ValueError, KeyError, TypeError):
        return False  # a record that cannot be read proves nothing
    try:
        process = psutil.Process(pid)
        if abs(_started(process) - created) > _SAME_PROCESS_SECONDS:
            return True  # the pid is someone else's now: ours is gone
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        if not kill:
            return False
        process.kill()
        try:
            process.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            return process.status() == psutil.STATUS_ZOMBIE
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
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


def _launched(scope: Path, name: str) -> bool:
    return (scope / f"{name}.launch").exists() or (scope / f"{name}.pid.part").exists()


def _attempts(generation: Path) -> list[Path]:
    service = generation / "service"
    if not service.is_dir():
        return []
    return sorted(path for path in service.iterdir() if path.is_dir() and not path.is_symlink())


def generation_gone(generation: Path, *, timeout: float = 5) -> bool:
    """An EARLIER run of main is over: nothing it started can still read a source or render (final review C2,
    consilium E3-I-R1).

    - Its owner -- that main -- has ended. It is never killed: a live one is another main, not ours to touch.
      Without an owner record the run is over only if it launched nothing (main records itself first).
    - Every process it recorded has ended, killed if need be, PARENTS FIRST: the worker guardian, the
      worker, then each attempt's guardian, part_render and Node. A parent proven dead spawns and records
      nothing more, so its children's records are final when they are read -- the observation holds over the
      whole interval, unlike a scan of the process table.
    - A launch without its record is a process that never got its input (records precede input): it spawned
      nothing and ends at the EOF of the pipe its parent held. It counts as over only when that parent is
      POSITIVELY proven dead -- recorded and ended, or itself launched-unrecorded under a parent proven dead.
      A parent that was never launched is no state this code writes: unprovable, so not over.
    """
    owner = generation / "owner.pid"
    if not owner.exists():
        scopes = [generation, *_attempts(generation)]
        return not any(
            _launched(scope, name) or (scope / f"{name}.pid").exists()
            for scope in scopes
            for name in (*GENERATION_RECORDS, *RECORDS)
        )
    if not _gone(owner, timeout, kill=False):
        return False  # that main still runs, or its record cannot be read
    settled: dict[tuple[Path, str], bool] = {}

    def over(scope: Path, name: str) -> bool:
        if (scope, name) not in settled:
            if (scope / f"{name}.pid").exists():
                settled[(scope, name)] = _gone(scope / f"{name}.pid", timeout)
            elif _launched(scope, name):
                settled[(scope, name)] = parent_ended(scope, name)
            else:
                settled[(scope, name)] = True  # never launched
        return settled[(scope, name)]

    def parent_ended(scope: Path, name: str) -> bool:
        parent = _PARENT[name]
        if parent == "owner":
            return True  # proven above
        where = generation if parent in GENERATION_RECORDS else scope
        if (where / f"{parent}.pid").exists():
            return over(where, parent)
        return _launched(where, parent) and parent_ended(where, parent)

    if not all(over(generation, name) for name in GENERATION_RECORDS):
        return False
    return all(over(attempt, name) for attempt in _attempts(generation) for name in RECORDS)
