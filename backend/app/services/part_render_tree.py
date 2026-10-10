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

import errno
import json
import os
import re
import subprocess
import sys
import time
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
# POSIX shows every process's uid; Windows hides other users' names and command lines from a non-admin but
# shows every process's image name and creator. Each platform's scan reads what it can read of EVERY process.
_WINDOWS = os.name == "nt"
_IMAGES = {"python.exe", "pythonw.exe", "node.exe"} | {
    Path(exe).name.lower() for exe in (sys.executable, getattr(sys, "_base_executable", None)) if exe
}
_KERNEL_PIDS = {0, 4}  # Windows: the idle process and System
_LAUNCH = "--bamdude-launch="
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_DWORD_BOOT = re.compile(r"bootid-(0|[1-9][0-9]{0,9})\Z")
_WINDOWS_BOOT = sys.platform == "win32"  # the format this platform's reader writes: a counter, else a UUID
_END_ROUNDS = 4  # a venv launcher, the interpreter it starts, then a round that finds none -- and one spare
_WINDOWS_BOOT_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"
_REG_DWORD = 4
_BOOT: list[str | None] = []


def _uuid_boot(raw: str) -> str | None:
    """A Linux boot_id / macOS boot session UUID, or None when the answer is not one (consilium r3, R3.2)."""
    text = raw.strip().lower()
    return text if _UUID.fullmatch(text) else None


def _windows_boot(value: object, kind: int) -> str | None:
    """Windows' boot counter, or None unless it is the DWORD the key holds."""
    if kind != _REG_DWORD or not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFF:
        return None
    return f"bootid-{value}"


def _read_boot_id() -> str | None:
    """This boot's identity: Linux's boot_id, macOS's boot session UUID, Windows' boot counter."""
    try:
        if sys.platform.startswith("linux"):
            return _uuid_boot(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii"))
        if sys.platform == "darwin":
            done = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=5
            )
            return _uuid_boot(done.stdout) if done.returncode == 0 else None
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_BOOT_KEY) as key:
                return _windows_boot(*winreg.QueryValueEx(key, "BootId"))
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError):
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


def _uid(uids: object) -> int | tuple | None:
    """POSIX: the uid, when real, effective and saved are one -- every process of a run runs so, none of ours
    changes uid. Mixed uids (a setuid program such as sudo, run by the same user: real 501, effective 0 --
    measured on CI's macOS runner) are returned whole, and so never equal an owner's uid: positively not ours,
    whatever the real uid says. None when psutil could not read them."""
    real, effective, saved = (getattr(uids, name, None) for name in ("real", "effective", "saved"))
    if real is None:
        return None
    return real if real == effective == saved else (real, effective, saved)


def _identity(process: psutil.Process) -> int | str | None:
    """Who runs ``process``, for a record: its uid on POSIX (None unless its uids are one), its user name on
    Windows; None when it cannot be read."""
    try:
        if _WINDOWS:
            return process.username()
        uid = _uid(process.uids())
        return uid if isinstance(uid, int) else None
    except (psutil.Error, AttributeError):
        return None


def _identity_of(info: dict) -> int | str | tuple | None:
    """The same identity, out of a ``process_iter`` row (None where psutil could not read it)."""
    if _WINDOWS:
        return info.get("username")
    return _uid(info.get("uids"))


def record(path: Path, pid: int) -> None:
    """Write the record atomically: a reader sees the whole record or none. It names the process's user too:
    every process of a run runs as its owner, so a token holder of another user is a stranger."""
    process = psutil.Process(pid)
    created = _started(process)
    user = _identity(process)
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="ascii") as out:  # a .part left by a write that failed is no record
        json.dump({"pid": pid, "create_time": created, "boot": _boot_id(), "user": user}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(part, path)
    _fsync_dir(path.parent)  # durable before the process gets its input -- a power cut keeps the record


def _wait(process: psutil.Process, timeout: float) -> bool:
    """Wait for a killed process; True when it has ended -- a zombie included.

    psutil 7 waits for a process that is not our child through a pidfd, and the kernel answers EINVAL when the
    pid still exists without its task -- another parent reaping it at that moment, or the pid now naming a
    thread. psutil maps that to nothing and raises the bare OSError, which turned a finished kill into an
    error and the worker into ownership_uncertain. Then the process is asked again by its identity
    (``is_running`` compares the start too): gone or a stranger now, or a zombie, it has ended."""
    try:
        process.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        return process.status() == psutil.STATUS_ZOMBIE
    except OSError as exc:  # psutil's own errors are not OSError
        if exc.errno != errno.EINVAL:
            raise
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _gone(entry: Path, timeout: float, *, kill: bool = True, this_run: bool = False) -> bool:
    return _not_gone(entry, timeout, kill=kill, this_run=this_run) is None


def _state(process: psutil.Process) -> str:
    """A process's state for the parent log: psutil's status word, never its command line."""
    try:
        return process.status()
    except psutil.Error:
        return "unreadable"


def _not_gone(entry: Path, timeout: float, *, kill: bool = True, this_run: bool = False) -> str | None:
    """Why the recorded process is not shown ended, for the parent log; None when it has ended -- killed first
    when ``kill`` and it still runs. An exited process this
    process cannot reap (an orphan whose new parent never reaps: a container's PID 1 without a reaper) stays
    a zombie, and a zombie has ended (consilium E3-I-R3).

    A record of another boot has ended with that boot; its pid is never signalled (consilium r2, R2.2). A
    pid and a start identify a process only within one boot -- Linux counts the start from the boot, macOS
    and Windows by a wall clock that can repeat across one -- so a record that cannot be placed in THIS boot
    (written without its boot, or read where the boot cannot be told) shows its process gone when nothing
    has that pid and start, and otherwise proves nothing and signals nothing, on every platform (consilium
    r3, R3.2). ``this_run``: the record was written by this run -- main, or the worker -- which outlives no
    reboot, so it is of this boot by construction."""
    try:
        data = json.loads(entry.read_text(encoding="ascii"))
        pid, created = int(data["pid"]), float(data["create_time"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "record unreadable"  # a record that cannot be read proves nothing
    if not this_run and _another_boot(data):
        return None  # it ended with its boot; the pid is now some other process's -- never signal it
    placed = this_run or _this_boot(data)
    try:
        process = psutil.Process(pid)
        if abs(_started(process) - created) > _SAME_PROCESS_SECONDS:
            return None  # the pid is someone else's now: ours is gone
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        if not kill:
            return f"running pid={pid} status={_state(process)}"
        if not placed:
            return f"running pid={pid} status={_state(process)}, boot unknown"
        process.kill()
        if _wait(process, timeout):
            return None
        return f"still running after its kill pid={pid} status={_state(process)}"
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return f"access denied pid={pid}"


def tree_gone(attempt_dir: Path, *, strict: bool, timeout: float = 5) -> bool:
    """Every process of the attempt is gone.

    Every record must be proven dead. ``strict`` is main's proof after the worker itself died: a launch
    without its record -- or a half-written one -- may have produced a process outside every tree main can
    see, so it fails the proof and closes admission. Without ``strict``, as the worker asks right after
    ``PreviewProcess.stop()`` killed the attempt's group / Job Object, an unrecorded process was inside
    that group and went with it.
    """
    return tree_unproven(attempt_dir, strict=strict, timeout=timeout) is None


def tree_unproven(attempt_dir: Path, *, strict: bool, timeout: float = 5) -> str | None:
    """Why the attempt's processes are not proven gone -- ``<record>: <what failed>``, with a pid and its
    psutil status, never a command line or a path -- or None when they are (``tree_gone``). The reason is
    what the parent log needs to tell a slow kill from an unreadable record or a launch caught before its
    record (the local-microservice invariant)."""
    for name in RECORDS:
        pid_file = attempt_dir / f"{name}.pid"
        if pid_file.exists():
            why = _not_gone(pid_file, timeout, this_run=True)  # an attempt of this run: this boot's
            if why is not None:
                return f"{name}: {why}"
        elif strict and (attempt_dir / f"{name}.launch").exists():
            return f"{name}: launched without its record"
        elif strict and (attempt_dir / f"{name}.pid.part").exists():
            return f"{name}: half-written record"
    return None


def _known_boot(boot: object) -> str | None:
    """A boot identity in this platform's format -- what ``_read_boot_id`` writes -- else None. A persisted
    boot is checked like the reader's answer: other text is no observation of any boot (consilium r4, R4.2)."""
    if not isinstance(boot, str):
        return None
    if _WINDOWS_BOOT:
        counter = _DWORD_BOOT.fullmatch(boot)
        return boot if counter and int(counter.group(1)) <= 0xFFFFFFFF else None
    return boot if _UUID.fullmatch(boot) else None


def _boots(data: dict) -> tuple[str, str] | None:
    """The record's boot and this one, when both are known."""
    boot, now = _known_boot(data.get("boot")), _boot_id()  # the reader checks its own answer
    if boot is None or now is None:
        return None
    return boot, now


def _another_boot(data: dict) -> bool:
    boots = _boots(data)
    return boots is not None and boots[0] != boots[1]


def _this_boot(data: dict) -> bool:
    boots = _boots(data)
    return boots is not None and boots[0] == boots[1]


def _token(scope: Path, name: str) -> str | None:
    try:
        token = (scope / f"{name}.launch").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return token if _TOKEN.fullmatch(token) else None


def _another_program(info: dict) -> bool:
    """Windows: the process positively runs something no launch of ours runs -- its image name (read off the
    system's process list, for every process) is not one of ours, or the kernel started it (System and the
    idle process, whose pids are never reused)."""
    name = info.get("name")
    if name and name.lower() not in _IMAGES:
        return True
    return info.get("ppid") in _KERNEL_PIDS


def _holders(marker: str, owner: int | str | None) -> list[psutil.Process] | None:
    """Every process carrying ``marker``; None when a process that may carry it cannot be read.

    Only a positive observation excludes a process (consilium r3, R3.1) -- psutil answers None for what it
    may not read, and None is not absence:
    - an identity read and different from the run's ``owner`` (the uid POSIX shows for every process; the
      user name where Windows shows it): another user's process, a stranger that may have read the token off
      a public command line -- never ours, never signalled (security review of 2e0b445). An identity that
      cannot be read excludes nothing;
    - on Windows, a process running another program (``_another_program``) -- a launch of ours runs Python
      or Node, under the command line that carries its token;
    - a zombie, which has ended.
    Any other process whose command line cannot be read may be ours: the scan is no proof of absence.

    The token is public, so carrying it is no authority to signal (consilium r4, R4.1): a holder is returned
    -- to be killed -- only when its identity is READ and IS the run's owner. A holder whose identity cannot
    be read, or any holder of a run whose owner names no identity, keeps the scan unproven, untouched."""
    attrs = ["cmdline", "username", "name", "ppid"] if _WINDOWS else ["cmdline", "uids"]
    found = []
    for process in psutil.process_iter(attrs):
        identity = _identity_of(process.info)
        if owner is not None and identity is not None and identity != owner:
            continue
        cmdline = process.info.get("cmdline")
        if cmdline is not None:
            if marker in cmdline:
                if owner is None or identity is None:
                    return None  # nothing shows it is ours: no proof, no signal
                found.append(process)
            continue
        if _WINDOWS and _another_program(process.info):
            continue
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            pass
        return None
    return found


def _end_launched(token: str, timeout: float, owner: int | str | None) -> bool:
    """End every process started with this launch's token; True when a scan finds none left running.

    Called only once the launch's parent is proven dead, so the token can be held only by what that parent
    started and by what THOSE started before they got any input -- on Windows the venv launcher starts the
    real interpreter with the same command line. Each round kills and waits for every holder it finds; a
    holder dead by the end of a round starts nothing more, so a round that finds none closes the set. A
    zombie has ended. A round that cannot read a process that may be a holder proves nothing (``_holders``).

    ``owner`` is the run's user, as its owner record names it: without it no process is a stranger, and no
    holder is shown to be ours -- one found keeps the run unproven."""
    marker = launch_arg(token)
    for _ in range(_END_ROUNDS):
        found = _holders(marker, owner)
        if found is None:
            return False  # a process that may hold the token cannot be read: not proven (consilium r3, R3.1)
        alive = False
        for process in found:
            try:
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue
                alive = True
                process.kill()
                if not _wait(process, timeout):
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
    user = data.get("user")
    if not (isinstance(user, str) if _WINDOWS else isinstance(user, int) and not isinstance(user, bool)):
        user = None  # no identity this platform reads: no process is a stranger -- fail closed
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
