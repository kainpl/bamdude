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
main itself died -- by records, parents first, never by a bare scan of the process table (consilium E3-I-R1).

Every ``<name>.launch`` holds a one-off token the parent puts on the child's command line (``launch_arg``),
so a child whose record never reached the disk can still be found and ENDED once its parent is proven dead
(consilium r2, R2.1). Every record names its boot (``_boot_id``): a record of another boot has ended and its
pid -- now some other process's -- is never signalled (consilium r2, R2.2).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
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
_LAUNCH = "--bamdude-launch="
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_END_ROUNDS = 4  # a venv launcher, the interpreter it starts, then a round that finds none -- and one spare
_WINDOWS_BOOT_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"
_BOOT: list[str | None] = []


def _read_boot_id() -> str | None:
    """This boot's identity: Linux's boot_id, macOS's boot session UUID, Windows' boot counter."""
    try:
        if sys.platform.startswith("linux"):
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
        if sys.platform == "darwin":
            done = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=5
            )
            return done.stdout.strip() or None
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_BOOT_KEY) as key:
                return f"bootid-{winreg.QueryValueEx(key, 'BootId')[0]}"
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def _boot_id() -> str | None:
    """This boot's identity, read once per process (it cannot change under it); None when the OS will not say."""
    if not _BOOT:
        _BOOT.append(_read_boot_id())
    return _BOOT[0]


def launch_arg(token: str) -> str:
    """The argument a parent adds to its child's command line: how the child is found without its record."""
    return f"{_LAUNCH}{token}"


def _started(process: psutil.Process) -> float:
    """When ``process`` started, comparable across processes and across a clock step."""
    if _BOOT_RELATIVE:
        return process.create_time() - psutil.boot_time()
    return process.create_time()


def launch(attempt_dir: Path, name: str) -> str:
    """Written by a parent before it spawns ``name``: from here on a missing record is unknown, not absent.
    Returns the launch's token, which the parent puts on the child's command line (``launch_arg``)."""
    token = uuid.uuid4().hex
    part = attempt_dir / f"{name}.launch.part"
    part.write_text(token, encoding="ascii")
    os.replace(part, attempt_dir / f"{name}.launch")  # a reader sees the whole token or no launch
    return token


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return  # Windows cannot open a directory for fsync; NTFS journals the rename
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record(path: Path, pid: int) -> None:
    """Write the record atomically: a reader sees the whole record or none. It names the process's user too:
    every process of a run runs as its owner, so a token holder of another user is a stranger."""
    process = psutil.Process(pid)
    created = _started(process)
    try:
        user = process.username()
    except psutil.Error:
        user = None
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="ascii") as out:  # a .part left by a write that failed is no record
        json.dump({"pid": pid, "create_time": created, "boot": _boot_id(), "user": user}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(part, path)
    _fsync_dir(path.parent)  # durable before the process gets its input -- a power cut keeps the record


def _gone(entry: Path, timeout: float, *, kill: bool = True) -> bool:
    """The recorded process has ended -- killed first when ``kill`` and it still runs. An exited process this
    process cannot reap (an orphan whose new parent never reaps: a container's PID 1 without a reaper) stays
    a zombie, and a zombie has ended (consilium E3-I-R3).

    A record of another boot has ended with that boot; its pid is never signalled (consilium r2, R2.2). On
    Linux the start is counted from a boot, so a record that cannot be placed in one -- written without its
    boot, or read where the boot cannot be told -- matches a process of ANY boot: it shows the process gone
    when nothing has its pid and start, and otherwise proves nothing and signals nothing."""
    try:
        data = json.loads(entry.read_text(encoding="ascii"))
        pid, created = int(data["pid"]), float(data["create_time"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False  # a record that cannot be read proves nothing
    if _another_boot(data):
        return True  # it ended with its boot; the pid is now some other process's -- never signal it
    unplaced = _BOOT_RELATIVE and (data.get("boot") is None or _boot_id() is None)
    try:
        process = psutil.Process(pid)
        if abs(_started(process) - created) > _SAME_PROCESS_SECONDS:
            return True  # the pid is someone else's now: ours is gone
        if process.status() == psutil.STATUS_ZOMBIE:
            return True
        if not kill or unplaced:
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


def _another_boot(data: dict) -> bool:
    boot, now = data.get("boot"), _boot_id()
    return boot is not None and now is not None and boot != now


def _token(scope: Path, name: str) -> str | None:
    try:
        token = (scope / f"{name}.launch").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return token if _TOKEN.fullmatch(token) else None


def _end_launched(token: str, timeout: float, user: str | None = None) -> bool:
    """End every process started with this launch's token; True when a scan finds none left running.

    Called only once the launch's parent is proven dead, so the token can be held only by what that parent
    started and by what THOSE started before they got any input -- on Windows the venv launcher starts the
    real interpreter with the same command line. Each round kills and waits for every holder it finds; a
    holder dead by the end of a round starts nothing more, so a round that finds none closes the set. A
    zombie has ended.

    A command line is public, so anyone on the machine can start a process carrying the token. Every
    process of a run runs as its owner's ``user``: a holder of another user is a stranger that read it, and
    is neither signalled nor allowed to keep the run unproven (security review of 2e0b445). Without a known
    user every holder counts -- fail closed. A process whose command line cannot be read is another user's."""
    marker = launch_arg(token)
    for _ in range(_END_ROUNDS):
        found = [
            p
            for p in psutil.process_iter(["cmdline", "username"])
            if marker in (p.info.get("cmdline") or []) and (user is None or p.info.get("username") == user)
        ]
        alive = False
        for process in found:
            try:
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue
                alive = True
                process.kill()
                try:
                    process.wait(timeout=timeout)
                except psutil.TimeoutExpired:
                    if process.status() != psutil.STATUS_ZOMBIE:
                        return False
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                return False
        if not alive:
            return True
    return False  # still finding holders: not proven


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
    - A launch without its record is a process that never got its input (records precede input) and so
      started nothing. Its parent must be POSITIVELY proven dead -- recorded and ended, or itself
      launched-unrecorded under a parent proven dead -- and then the process itself is found by its launch
      token and ENDED: a pending EOF is not an exit (consilium r2, R2.1). Every launch's token holders are
      ended once the launch is, which also ends a fork of it caught before it executed its child -- that
      fork still runs the parent's command line. A launch without a readable token, or with a parent that
      was never launched or whose token cannot be read, is no state this code writes: unprovable, not over.
      Main is not launched by us: its own fork caught before executing the worker guardian is outside every
      token, and can only become a guardian that reads EOF where its bootstrap should be and exits.
    - A run whose owner record names another boot is over whole: nothing survives a reboot, whatever a
      power cut left of its other files (consilium r2, R2.2).
    """
    owner = generation / "owner.pid"
    if not owner.exists():
        scopes = [generation, *_attempts(generation)]
        return not any(
            _launched(scope, name) or (scope / f"{name}.pid").exists()
            for scope in scopes
            for name in (*GENERATION_RECORDS, *RECORDS)
        )
    try:
        data = json.loads(owner.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if _another_boot(data):
        return True  # a run of an earlier boot: nothing of it exists any more
    user = data.get("user") if isinstance(data.get("user"), str) else None
    if not _gone(owner, timeout, kill=False):
        return False  # that main still runs, or its record cannot be read
    settled: dict[tuple[Path, str], bool] = {}

    def over(scope: Path, name: str) -> bool:
        if (scope, name) not in settled:
            settled[(scope, name)] = ended(scope, name)
        return settled[(scope, name)]

    def ended(scope: Path, name: str) -> bool:
        recorded = (scope / f"{name}.pid").exists()
        if not recorded and not _launched(scope, name):
            return True  # never launched
        token = _token(scope, name)
        if recorded:
            if not _gone(scope / f"{name}.pid", timeout):
                return False
        elif token is None or not parent_ended(scope, name):
            return False
        # whatever still holds the launch's token: the launched process itself when unrecorded, the
        # interpreter behind a Windows venv launcher, a fork of it that has not yet executed its own child
        return token is None or _end_launched(token, timeout, user)

    def parent_ended(scope: Path, name: str) -> bool:
        parent = _PARENT[name]
        if parent == "owner":
            return True  # proven above
        where = generation if parent in GENERATION_RECORDS else scope
        if not ((where / f"{parent}.pid").exists() or _launched(where, parent)):
            return False  # a parent that was never launched: no state this code writes
        # until it executes, the child runs the PARENT's command line: only the parent's token finds it
        return over(where, parent) and _token(where, parent) is not None

    if not all(over(generation, name) for name in GENERATION_RECORDS):
        return False
    return all(over(attempt, name) for attempt in _attempts(generation) for name in RECORDS)
