"""part_render_tree: an attempt's processes are recorded by their parents and proven gone (plan E3, R12)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.services.part_render_tree import launch, record, tree_gone, tree_unproven

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
# boots in THIS platform's format: a persisted boot counts only in it (consilium r4, R4.2)
_BOOT_A, _BOOT_B = (
    ("bootid-41", "bootid-42")
    if sys.platform == "win32"
    else ("6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "7d2a0b3f-4c5e-4f60-9b0c-1d2e3f4a5b6c")
)
# the run's owner as each platform's records name it
_OWNER = "HOST\\me" if os.name == "nt" else 1000


def _spawn():
    return subprocess.Popen(SLEEP, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.mark.parametrize("strict", [True, False])
def test_an_attempt_that_never_began_a_launch_is_an_empty_tree(tmp_path, strict):
    assert tree_gone(tmp_path, strict=strict)


@pytest.mark.parametrize("strict", [True, False])
def test_a_recorded_live_process_is_killed_and_proven_gone(tmp_path, strict):
    process = _spawn()
    launch(tmp_path, "node")
    record(tmp_path / "node.pid", process.pid)
    assert tree_gone(tmp_path, strict=strict)
    assert process.wait(timeout=5) is not None


def test_a_recycled_pid_belongs_to_a_stranger_and_is_never_killed(tmp_path):
    process = _spawn()
    try:
        (tmp_path / "node.pid").write_text(json.dumps({"pid": process.pid, "create_time": 1.0}), encoding="ascii")
        assert tree_gone(tmp_path, strict=True)
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize("strict", [True, False])
def test_a_record_that_cannot_be_read_proves_nothing(tmp_path, strict):
    (tmp_path / "child.pid").write_text("{broken", encoding="ascii")
    assert not tree_gone(tmp_path, strict=strict)


@pytest.mark.parametrize("name", ["guardian", "child", "node"])
def test_a_launch_without_its_record_fails_the_strict_proof(tmp_path, name):
    """Consilium E3-R1: the process may exist unrecorded, outside every tree main can see."""
    launch(tmp_path, name)
    assert not tree_gone(tmp_path, strict=True)
    assert tree_gone(tmp_path, strict=False)  # the worker's own proof: stop() just killed that group / job


def test_a_half_written_record_fails_the_strict_proof(tmp_path):
    (tmp_path / "child.pid.part").write_text("{", encoding="ascii")
    assert not tree_gone(tmp_path, strict=True)
    assert tree_gone(tmp_path, strict=False)


# Why a proof failed, for the parent log: the record, the check and the process's state -- never a command
# line or a path (the local-microservice invariant; the macOS ownership_uncertain of 2026-10-10 said only
# "not proven gone", which cannot tell its four causes apart).


def test_a_proof_names_a_launch_without_its_record(tmp_path):
    launch(tmp_path, "child")
    assert tree_unproven(tmp_path, strict=True) == "child: launched without its record"  # no staging: no rule
    assert tree_unproven(tmp_path, strict=False) is None


def test_a_proof_names_a_half_written_record(tmp_path):
    (tmp_path / "guardian.pid.part").write_text("{", encoding="ascii")
    assert tree_unproven(tmp_path, strict=True) == "guardian: half-written record"


def test_a_proof_names_an_unreadable_record(tmp_path):
    (tmp_path / "node.pid").write_text("{broken", encoding="ascii")
    assert tree_unproven(tmp_path, strict=True) == "node: record unreadable"


def test_a_proof_names_a_process_that_outlived_its_kill(tmp_path, monkeypatch):
    from backend.app.services import part_render_tree

    process = _spawn()
    try:
        record(tmp_path / "child.pid", process.pid)
        monkeypatch.setattr(part_render_tree, "_wait", lambda _process, _timeout: False)
        why = tree_unproven(tmp_path, strict=True)
        assert why is not None and why.startswith(f"child: still running after its kill pid={process.pid} status=")
    finally:
        process.kill()
        process.wait()


def test_a_proof_names_a_process_it_may_not_read(tmp_path, monkeypatch):
    import psutil

    from backend.app.services import part_render_tree

    process = _spawn()
    try:
        record(tmp_path / "child.pid", process.pid)

        def denied(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(part_render_tree.psutil, "Process", denied)
        assert tree_unproven(tmp_path, strict=True) == f"child: access denied pid={process.pid}"
    finally:
        process.kill()
        process.wait()


def test_a_proven_tree_has_no_reason(tmp_path):
    process = _spawn()
    launch(tmp_path, "child")
    record(tmp_path / "child.pid", process.pid)
    assert tree_unproven(tmp_path, strict=True) is None
    assert process.wait(timeout=5) is not None


def test_a_process_that_already_exited_is_gone(tmp_path):
    process = _spawn()
    launch(tmp_path, "child")
    record(tmp_path / "child.pid", process.pid)
    process.kill()
    process.wait()
    assert tree_gone(tmp_path, strict=True)


def test_a_clock_step_does_not_make_a_live_process_look_like_a_stranger(tmp_path, monkeypatch):
    """Final review M2: on Linux create_time is boot time plus ticks, and the boot time moves when the clock is
    stepped (an RTC-less Pi syncing NTP after start). The record keeps the distance from boot there, so the
    proof still kills the process instead of passing it as a recycled pid."""
    import psutil

    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_BOOT_RELATIVE", True)
    monkeypatch.setattr(tree, "_boot_id", lambda: _BOOT_A)  # the same boot: only the clock moved
    process = _spawn()
    try:
        record(tmp_path / "node.pid", process.pid)
        real_boot, real_create = psutil.boot_time, psutil.Process.create_time
        monkeypatch.setattr(psutil, "boot_time", lambda: real_boot() + 3600.0)  # the clock was stepped an hour
        monkeypatch.setattr(psutil.Process, "create_time", lambda self: real_create(self) + 3600.0)
        assert tree_gone(tmp_path, strict=True)
        assert process.wait(timeout=5) is not None  # killed by the proof, not waved through
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _dead(path: Path) -> None:
    """The record of a process that has ended."""
    process = _spawn()
    record(path, process.pid)
    process.kill()
    process.wait()


def _generation(tmp_path: Path) -> tuple[Path, Path]:
    generation = tmp_path / ("e" * 32)
    attempt = generation / "service" / ("f" * 32)
    attempt.mkdir(parents=True)
    return generation, attempt


def test_an_earlier_generation_is_over_when_its_records_are_dead_and_an_unrecorded_launch_had_a_dead_parent(
    tmp_path,
):
    """Consilium E3-I-R1: main died inside a spawn window (node.launch, no record). The parent that would have
    given Node its input is proven dead, so Node never got it: records precede input."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    for scope, name in ((generation, "owner"), (generation, "worker"), (generation, "service")):
        _dead(scope / f"{name}.pid")
    for name in ("guardian", "child"):
        launch(attempt, name)
        _dead(attempt / f"{name}.pid")
    launch(attempt, "node")
    assert generation_gone(generation)


def test_an_unrecorded_launch_whose_parent_is_not_proven_dead_is_not_over(tmp_path):
    """A node.launch whose part_render was never recorded nor launched is no state this code writes."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    _dead(generation / "owner.pid")
    launch(attempt, "node")
    assert not generation_gone(generation)


@pytest.mark.parametrize("scan_why", [None, "a possible holder unreadable"], ids=["clear-scan", "unproven-scan"])
def test_a_live_recorded_process_of_an_earlier_generation_is_killed_not_waved_through(tmp_path, monkeypatch, scan_why):
    """Consilium E3-I-R1: valid positive evidence of a live process is acted on, whatever any scan says."""
    from backend.app.services import part_render_tree as tree

    generation, attempt = _generation(tmp_path)
    _dead(generation / "owner.pid")
    alive = _spawn()
    scans = []

    def holders(marker, owner):
        # The record must kill its real process BEFORE the token scan, even when
        # that scan cannot exclude another holder. The host's unrelated processes
        # are not the subject of this test; real scans have their own orphan tests.
        assert alive.poll() is not None
        scans.append((marker, owner))
        return [], scan_why

    monkeypatch.setattr(tree, "_holders_why", holders)
    try:
        token = launch(attempt, "node")
        record(attempt / "node.pid", alive.pid)
        assert tree.generation_gone(generation) is (scan_why is None)
        assert alive.wait(timeout=5) is not None  # killed by the proof
        assert scans == [(tree.launch_arg(token), tree._run_owner(generation))]
    finally:
        if alive.poll() is None:
            alive.kill()
            alive.wait()


def test_an_earlier_generation_whose_main_still_lives_is_not_over_and_nothing_of_it_is_killed(tmp_path):
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    owner, node = _spawn(), _spawn()
    try:
        record(generation / "owner.pid", owner.pid)
        record(attempt / "node.pid", node.pid)
        assert not generation_gone(generation)
        assert owner.poll() is None and node.poll() is None  # another main's processes are not ours to kill
    finally:
        for process in (owner, node):
            process.kill()
            process.wait()


def test_a_generation_without_its_owner_record_is_over_only_when_nothing_was_launched(tmp_path):
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    assert generation_gone(generation)  # main died before it recorded itself, so before any spawn
    launch(attempt, "node")
    assert not generation_gone(generation)


def test_a_killed_process_that_stays_a_zombie_is_proven_stopped(tmp_path, monkeypatch):
    """Consilium E3-I-R3: an orphan killed by the check becomes a zombie its new parent (a container's PID 1
    without a reaper) never reaps; psutil.wait() times out because this process is not its parent."""
    import json

    import psutil

    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_BOOT_RELATIVE", False)

    class Orphan:
        killed = False

        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 1000.0

        def status(self):
            return psutil.STATUS_ZOMBIE if Orphan.killed else psutil.STATUS_RUNNING

        def kill(self):
            Orphan.killed = True

        def wait(self, timeout=None):
            raise psutil.TimeoutExpired(timeout)

    monkeypatch.setattr(tree.psutil, "Process", Orphan)
    monkeypatch.setattr(tree, "_boot_id", lambda: _BOOT_A)
    entry = tmp_path / "node.pid"
    entry.write_text(json.dumps({"pid": 4242, "create_time": 1000.0, "boot": _BOOT_A}), encoding="ascii")
    assert tree._gone(entry, timeout=0.1)
    assert Orphan.killed


@pytest.mark.skipif(os.name == "nt", reason="a reparented orphan is a POSIX state")
def test_an_orphan_killed_by_the_record_check_is_proven_stopped(tmp_path):
    """Consilium E3-I-R3, on real processes: the orphan's new parent may never reap it."""
    import psutil

    from backend.app.services import part_render_tree as tree

    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
    )
    orphan = int(subprocess.check_output([sys.executable, "-c", code]))
    entry = tmp_path / "node.pid"
    try:
        record(entry, orphan)
        assert tree._gone(entry, timeout=0.25)
        try:
            assert psutil.Process(orphan).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        try:
            psutil.Process(orphan).kill()
        except psutil.NoSuchProcess:
            pass


def _launched_by_a_dead_parent(attempt: Path, name: str) -> subprocess.Popen:
    """What a parent that died inside the spawn window leaves: <name>.launch with its token, the process
    started with the token on its command line, and no record."""
    from backend.app.services.part_render_tree import launch_arg

    token = launch(attempt, name)
    return subprocess.Popen([*SLEEP, launch_arg(token)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _dead_ancestors(generation: Path, attempt: Path) -> None:
    for name in ("owner", "worker", "service"):
        _dead(generation / f"{name}.pid")
    for name in ("guardian", "child"):
        launch(attempt, name)
        _dead(attempt / f"{name}.pid")


def scan_test_launch(monkeypatch, pid):
    # The POSIX tests start one interpreter. Scan its actual command line and
    # identity without letting unrelated processes on the runner affect the
    # proof. Windows still scans for both its venv launcher and interpreter.
    if os.name == "nt":
        return
    import psutil

    process_iter = psutil.process_iter

    def scan(attrs=None):
        return (process for process in process_iter(attrs) if process.pid == pid)

    monkeypatch.setattr(psutil, "process_iter", scan)


def test_an_unrecorded_launch_under_a_dead_parent_is_found_by_its_token_and_ended(tmp_path, monkeypatch):
    """Consilium r2, R2.1: a dead parent proves the input was withheld, not that the child exited. The child
    is found by the token its launch carries -- no new one can be born once the parent is dead -- and ended."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    unrecorded = _launched_by_a_dead_parent(attempt, "node")
    scan_test_launch(monkeypatch, unrecorded.pid)
    try:
        assert generation_gone(generation)
        assert unrecorded.wait(timeout=5) is not None  # ended by the proof, not waited out
        assert _holders((attempt / "node.launch").read_text(encoding="ascii")) == []  # a venv's interpreter too
    finally:
        if unrecorded.poll() is None:
            unrecorded.kill()
            unrecorded.wait()


def test_an_unrecorded_launch_without_a_readable_token_is_not_over(tmp_path):
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    (attempt / "node.launch").write_text("", encoding="ascii")  # a launch whose token never reached the disk
    assert not generation_gone(generation)


@pytest.mark.skipif(os.name == "nt", reason="SIGSTOP holds Node before it reads EOF (POSIX)")
def test_a_stopped_real_node_launched_without_a_record_is_ended_by_the_proof(tmp_path, monkeypatch):
    """Consilium r2, R2.1 with the pinned Node and the production command: stopped before it could read EOF,
    its parent dead -- the proof ends it instead of trusting the EOF."""
    import signal
    import time

    import psutil

    from backend.app.services.part_render_tree import generation_gone, launch_arg
    from backend.tests.unit.services.test_part_render_node import NODE

    if NODE is None:
        pytest.skip("no pinned Node")
    generation, attempt = _generation(tmp_path)
    for name in ("owner", "worker", "service"):
        _dead(generation / f"{name}.pid")
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    child_token = launch(attempt, "child")  # its guardian launched it with this token (consilium r3, R3.3)
    code = (
        "import os, signal, subprocess, sys\n"
        "from pathlib import Path\n"
        "from backend.app.services.part_render_node import node_command, node_env\n"
        "from backend.app.services.part_render_tree import launch, launch_arg, record\n"
        "root = Path(sys.argv[1])\n"
        "record(root / 'child.pid', os.getpid())\n"
        "token = launch(root, 'node')\n"
        "p = subprocess.Popen(node_command(Path(sys.argv[2])) + [launch_arg(token)], stdin=subprocess.PIPE,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=node_env())\n"
        "os.kill(p.pid, signal.SIGSTOP)\n"
        "print(p.pid, flush=True)\n"
        "os._exit(0)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(attempt), str(NODE), launch_arg(child_token)], stdout=subprocess.PIPE
    )
    node_pid = int(parent.stdout.readline())
    parent.wait(timeout=10)
    scan_test_launch(monkeypatch, node_pid)
    try:
        # SIGSTOP takes effect when the kernel next schedules Node: under load its status still reads "running" a
        # moment after the parent sent it (Backend Tests, -n 4, 2026-10-11). The precondition waits for the stop.
        deadline = time.monotonic() + 5
        while psutil.Process(node_pid).status() != psutil.STATUS_STOPPED:
            assert time.monotonic() < deadline, "Node never showed stopped"
            time.sleep(0.01)
        assert generation_gone(generation)
        assert not psutil.pid_exists(node_pid) or psutil.Process(node_pid).status() == psutil.STATUS_ZOMBIE
    finally:
        try:
            psutil.Process(node_pid).kill()
        except psutil.NoSuchProcess:
            pass


class _Kernel:
    """A modelled OS: the boot the reader reports, and whether anything was signalled."""

    boot = _BOOT_A
    killed = False


def _model(monkeypatch, *, ticks: float = 25.0):
    import psutil

    from backend.app.services import part_render_tree as tree

    _Kernel.boot, _Kernel.killed = _BOOT_A, False

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 1000.0 + ticks

        def username(self):
            return "me"

        def status(self):
            return psutil.STATUS_RUNNING

        def kill(self):
            _Kernel.killed = True

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(tree, "_BOOT_RELATIVE", True)
    monkeypatch.setattr(tree, "_boot_id", lambda: _Kernel.boot)
    monkeypatch.setattr(tree.psutil, "Process", Process)
    monkeypatch.setattr(tree.psutil, "boot_time", lambda: 1000.0)
    return tree


def test_a_record_from_another_boot_has_ended_and_its_pid_is_never_signalled(tmp_path, monkeypatch):
    """Consilium r2, R2.2: the same pid at the same tick after a reboot is another process."""
    tree = _model(monkeypatch)
    tree.record(tmp_path / "node.pid", 4242)
    _Kernel.boot = _BOOT_B
    assert tree._gone(tmp_path / "node.pid", timeout=0.1)
    assert not _Kernel.killed
    assert tree._gone(tmp_path / "node.pid", timeout=0.1, kill=False)  # an earlier boot's main has ended too


def test_a_record_from_this_boot_still_kills_its_live_process(tmp_path, monkeypatch):
    tree = _model(monkeypatch)
    tree.record(tmp_path / "node.pid", 4242)
    assert tree._gone(tmp_path / "node.pid", timeout=0.1)
    assert _Kernel.killed


def test_a_linux_record_without_its_boot_proves_nothing_and_signals_nothing(tmp_path, monkeypatch):
    """Consilium r2, R2.2: a record from before the boot was stored cannot be placed in a boot."""
    import json

    tree = _model(monkeypatch)
    (tmp_path / "node.pid").write_text(json.dumps({"pid": 4242, "create_time": 25.0}), encoding="ascii")
    assert not tree._gone(tmp_path / "node.pid", timeout=0.1)
    assert not _Kernel.killed


def test_a_linux_record_without_its_boot_shows_its_process_gone_when_nothing_could_be_it(tmp_path, monkeypatch):
    """Consilium r2, R2.2, records written before the boot was stored: when no process has the recorded pid
    and start, the recorded one is gone in any boot -- nothing to signal, nothing ambiguous."""
    import psutil

    tree = _model(monkeypatch)
    entry = tmp_path / "node.pid"
    entry.write_text(json.dumps({"pid": 4242, "create_time": 99.0}), encoding="ascii")
    assert tree._gone(entry, timeout=0.1)  # the pid is held by a process with another start
    assert not _Kernel.killed

    def no_such(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(tree.psutil, "Process", no_such)
    entry.write_text(json.dumps({"pid": 4242, "create_time": 25.0}), encoding="ascii")
    assert tree._gone(entry, timeout=0.1)


def test_a_run_of_another_boot_is_over_whatever_a_power_cut_left_of_its_files(tmp_path, monkeypatch):
    """Consilium r2, R2.2: nothing of a run outlives its boot. A launch whose token or record a power cut lost
    must not keep the runtime closed, and no pid the run recorded is signalled."""
    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_boot_id", lambda: _BOOT_A)
    generation, attempt = _generation(tmp_path)
    stranger = _spawn()  # what holds the recorded pids in the next boot
    try:
        record(generation / "owner.pid", stranger.pid)
        record(generation / "worker.pid", stranger.pid)
        (attempt / "node.launch").write_text("", encoding="ascii")  # its token lost
        (attempt / "child.pid.part").write_text("{", encoding="ascii")  # its record torn
        monkeypatch.setattr(tree, "_boot_id", lambda: _BOOT_B)
        assert tree.generation_gone(generation)
        assert stranger.poll() is None  # never signalled
    finally:
        stranger.kill()
        stranger.wait()


def _holders(token: str) -> list:
    """Every process still running with this launch's token on its command line."""
    import psutil

    from backend.app.services.part_render_tree import launch_arg

    marker = launch_arg(token)
    return [
        p
        for p in psutil.process_iter(["cmdline", "status"])
        if marker in (p.info["cmdline"] or []) and p.info["status"] != psutil.STATUS_ZOMBIE
    ]


def test_a_fork_caught_before_it_executed_its_child_is_ended_by_its_parents_token(tmp_path, monkeypatch):
    """Consilium r2, R2.1: until it executes, a child runs its PARENT's command line, so the child's own token
    cannot find it -- a parent killed while that exec stalls leaves exactly this. Proving the parent over ends
    every holder of the parent's token; here also the interpreter behind a Windows venv launcher."""
    from backend.app.services.part_render_tree import generation_gone, launch_arg

    generation, attempt = _generation(tmp_path)
    for name in ("owner", "worker", "service"):
        _dead(generation / f"{name}.pid")
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    token = launch(attempt, "child")
    _dead(attempt / "child.pid")
    launch(attempt, "node")  # its spawn began: node.launch and no record
    fork = subprocess.Popen([*SLEEP, launch_arg(token)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    scan_test_launch(monkeypatch, fork.pid)
    try:
        assert generation_gone(generation)
        assert fork.wait(timeout=5) is not None
        assert _holders(token) == []
    finally:
        if fork.poll() is None:
            fork.kill()
            fork.wait()


def test_an_unrecorded_launch_whose_parents_token_cannot_be_read_is_not_over(tmp_path):
    """Without the parent's token a fork of it caught before its exec cannot be found: not proven."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    (attempt / "child.launch").write_text("", encoding="ascii")
    launch(attempt, "node")
    assert not generation_gone(generation)


class _Holder:
    killed: list = []

    def __init__(self, pid: int):
        from backend.app.services.part_render_tree import launch_arg

        self.pid = pid
        self.info = {"cmdline": ["python", launch_arg("a" * 32)], "username": _OWNER, "uids": _Uids(1000, 1000, 1000)}

    def status(self):
        import psutil

        return psutil.STATUS_RUNNING

    def kill(self):
        _Holder.killed.append(self.pid)

    def wait(self, timeout=None):
        return 0


def test_a_holder_started_by_a_holder_during_the_scan_is_found_by_the_next_round(monkeypatch):
    """A venv launcher may start its interpreter -- the same command line -- after a round saw only the
    launcher: the next round finds it, and only a round that finds none closes the set."""
    from backend.app.services import part_render_tree as tree

    _Holder.killed = []
    rounds = iter([[_Holder(1)], [_Holder(2)], []])
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: next(rounds))
    assert tree._end_launched("a" * 32, 0.1, _OWNER)
    assert _Holder.killed == [1, 2]


def test_holders_that_keep_appearing_are_not_proven_ended(monkeypatch):
    from backend.app.services import part_render_tree as tree

    _Holder.killed = []
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: [_Holder(len(_Holder.killed) + 1)])
    assert not tree._end_launched("a" * 32, 0.1, _OWNER)


_Uids = __import__("collections").namedtuple("puids", "real effective saved")


class _Stranger:
    """A process of another local user started with a token it read off our command line (argv is public)."""

    touched = False

    def __init__(self, token: str):
        from backend.app.services.part_render_tree import launch_arg

        self.pid = 4242
        self.info = {
            "cmdline": ["sleep", launch_arg(token)],
            "username": "mallory",
            "uids": _Uids(4242, 4242, 4242),
            "ppid": 1,
        }

    def status(self):
        import psutil

        return psutil.STATUS_RUNNING

    def kill(self):
        import psutil

        _Stranger.touched = True
        raise psutil.AccessDenied(self.pid)

    def wait(self, timeout=None):
        return 0


def test_a_token_holder_of_another_user_never_blocks_the_proof_nor_is_touched(tmp_path, monkeypatch):
    """Security review of 2e0b445: the token is on a command line every local user can read. Another user's
    process carrying it is a stranger -- the run's processes all run as its owner -- so it can neither keep
    the run unproven (a kill it refuses) nor be signalled."""
    from backend.app.services import part_render_tree as tree

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    token = launch(attempt, "node")
    _Stranger.touched = False
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: [_Stranger(token)])
    assert tree.generation_gone(generation)
    assert not _Stranger.touched


def test_a_run_whose_owner_names_no_user_has_no_authority_over_a_token_holder(tmp_path, monkeypatch):
    """Consilium r4, R4.1: without the run's user nothing shows a token holder is ours -- the token is public.
    The holder keeps the run unproven and is not signalled."""
    from backend.app.services import part_render_tree as tree

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    owner = json.loads((generation / "owner.pid").read_text(encoding="ascii"))
    owner.pop("user", None)
    (generation / "owner.pid").write_text(json.dumps(owner), encoding="ascii")
    token = launch(attempt, "node")
    _Stranger.touched = False
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: [_Stranger(token)])
    assert not tree.generation_gone(generation)
    assert not _Stranger.touched


def _orphan_under_a_recorded_parent(generation: Path, attempt: Path) -> tuple[int, str]:
    """What a part_render that died inside its spawn window leaves, in the production order: its guardian
    launched it with its token and recorded it; it launched Node with Node's token and died before recording
    it. The orphan here is a sleeper, not Node -- same records, same command-line shape."""
    for name in ("owner", "worker", "service"):
        _dead(generation / f"{name}.pid")
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    from backend.app.services.part_render_tree import launch_arg

    child_token = launch(attempt, "child")
    code = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from backend.app.services.part_render_tree import launch, launch_arg\n"
        "token = launch(Path(sys.argv[1]), 'node')\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', launch_arg(token)],"
        " creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
        "print(p.pid, flush=True)\n"
        "sys.stdin.read()\n"
        "os._exit(0)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(attempt), launch_arg(child_token)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    record(attempt / "child.pid", parent.pid)  # its guardian's record, before it got any input
    orphan = int(parent.stdout.readline())
    parent.stdin.close()
    parent.wait(timeout=10)
    return orphan, (attempt / "node.launch").read_text(encoding="ascii")


def _end(pid: int) -> None:
    import psutil

    try:
        process = psutil.Process(pid)
        for each in [*process.children(recursive=True), process]:
            each.kill()
    except psutil.NoSuchProcess:
        pass


def test_an_unrecorded_launch_in_the_production_shape_is_found_and_ended(tmp_path, monkeypatch):
    """The parent ordering every launch follows, on every platform: the orphan is ended by its token."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    orphan, token = _orphan_under_a_recorded_parent(generation, attempt)
    scan_test_launch(monkeypatch, orphan)
    try:
        assert generation_gone(generation)
        assert _holders(token) == []
    finally:
        _end(orphan)


def test_an_unrecorded_holder_whose_command_line_cannot_be_read_keeps_the_run_unproven(tmp_path, monkeypatch):
    """Consilium r3, R3.1: psutil answers None for a command line it may not read, and None is no observation
    of absence. A process that may be ours and cannot be read keeps the run unproven and its records kept."""
    import psutil

    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    orphan, token = _orphan_under_a_recorded_parent(generation, attempt)
    scan_test_launch(monkeypatch, orphan)
    original = psutil.Process.cmdline

    @__import__("functools").wraps(original)
    def denied(process):
        if process.pid == orphan:
            raise psutil.AccessDenied(process.pid)
        return original(process)

    monkeypatch.setattr(psutil.Process, "cmdline", denied)
    try:
        assert not generation_gone(generation)
        assert (attempt / "node.launch").exists() and (attempt / "child.pid").exists()
    finally:
        monkeypatch.undo()
        _end(orphan)


def test_a_token_holder_whose_user_cannot_be_read_keeps_the_run_unproven_and_is_not_signalled(tmp_path, monkeypatch):
    """Consilium r4, R4.1: a hidden identity shows neither a stranger nor ours. The token is public, so it is no
    authority to signal: the holder keeps the run unproven, untouched."""
    import psutil

    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    orphan, token = _orphan_under_a_recorded_parent(generation, attempt)
    scan_test_launch(monkeypatch, orphan)
    attribute = "username" if os.name == "nt" else "uids"
    original = getattr(psutil.Process, attribute)

    @__import__("functools").wraps(original)  # keeps psutil's oneshot cache hooks on POSIX
    def denied(process):
        if process.pid == orphan:
            raise psutil.AccessDenied(process.pid)
        return original(process)

    monkeypatch.setattr(psutil.Process, attribute, denied)
    try:
        assert not generation_gone(generation)
        assert psutil.pid_exists(orphan) and psutil.Process(orphan).status() != psutil.STATUS_ZOMBIE
        assert (attempt / "node.launch").exists()
    finally:
        monkeypatch.undo()
        _end(orphan)


class _Unreadable:
    """A process whose command line psutil could not read, with the identity fields a platform reads."""

    def __init__(self, pid, *, uid=None, user=None, name=None, ppid=None, status="running"):
        self.pid = pid
        self._status = status
        self.info = {
            "cmdline": None,
            "uids": None if uid is None else _Uids(uid, uid, uid),
            "username": user,
            "name": name,
            "ppid": ppid,
        }

    def status(self):
        return self._status


def _scan(monkeypatch, processes, *, windows: bool):
    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_WINDOWS", windows)
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: list(processes))
    return tree


def test_another_users_unreadable_process_does_not_block_the_proof(monkeypatch):
    """POSIX shows every process's uid: a process positively of another user cannot hold the run's token,
    and its unreadable command line is not needed."""
    tree = _scan(monkeypatch, [_Unreadable(7, uid=4242)], windows=False)
    assert tree._end_launched("a" * 32, 0.1, 1000)


@pytest.mark.parametrize(
    "foreign",
    [
        {"name": "svchost.exe", "ppid": 1536},  # another program
        {"name": "", "ppid": 4},  # started by System: "Secure System" shows no name
        {"name": "python.exe", "user": "OTHER\\mallory", "ppid": 999},  # another user, where Windows says so
    ],
)
def test_a_windows_process_positively_not_ours_does_not_block_the_proof(monkeypatch, foreign):
    """Windows shows every process's image name and creator, and another user's name where it may: a
    process running another program, started by the kernel, or of another user cannot hold the token."""
    tree = _scan(monkeypatch, [_Unreadable(7, **foreign)], windows=True)
    assert tree._end_launched("a" * 32, 0.1, "HOST\\me")


@pytest.mark.parametrize(
    ("windows", "ours"),
    [
        (False, {"uid": 1000}),  # our uid
        (False, {}),  # a uid that could not be read either
        (True, {"name": "python.exe", "ppid": 6000}),  # our interpreter, user name hidden
        (True, {"name": "node.exe", "user": "HOST\\me", "ppid": 6000}),
        (True, {"name": None, "ppid": 6000}),  # no name read: not a positive observation
    ],
)
def test_an_unreadable_process_that_may_be_ours_is_no_proof(monkeypatch, windows, ours):
    tree = _scan(monkeypatch, [_Unreadable(7, **ours)], windows=windows)
    assert not tree._end_launched("a" * 32, 0.1, "HOST\\me" if windows else 1000)


def test_a_zombie_whose_command_line_cannot_be_read_has_ended(monkeypatch):
    import psutil

    tree = _scan(monkeypatch, [_Unreadable(7, uid=1000, status=psutil.STATUS_ZOMBIE)], windows=False)
    assert tree._end_launched("a" * 32, 0.1, 1000)


@pytest.mark.parametrize("boot_failure", ["record_boot_unknown", "reader_boot_unknown"])
def test_no_boot_identity_never_signals_an_ambiguous_pid_off_linux(tmp_path, monkeypatch, boot_failure):
    """Consilium r3, R3.2: macOS and Windows start times are wall clock, which can repeat across a reboot.
    Without both boots known, a process matching the record's pid and start proves nothing and is never
    signalled -- on every platform, not only Linux."""
    tree = _model(monkeypatch)
    monkeypatch.setattr(tree, "_BOOT_RELATIVE", False)
    monkeypatch.setattr(tree.psutil, "boot_time", lambda: 0.0)
    if boot_failure == "record_boot_unknown":
        _Kernel.boot = None
    entry = tmp_path / "node.pid"
    record(entry, 4242)
    _Kernel.boot = _BOOT_B if boot_failure == "record_boot_unknown" else None
    assert not tree._gone(entry, timeout=0.1)
    assert not _Kernel.killed


def test_an_unknown_boot_still_shows_a_process_gone_when_nothing_could_be_it(tmp_path, monkeypatch):
    import psutil

    tree = _model(monkeypatch)
    monkeypatch.setattr(tree, "_BOOT_RELATIVE", False)
    _Kernel.boot = None
    entry = tmp_path / "node.pid"
    record(entry, 4242)

    def no_such(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(tree.psutil, "Process", no_such)
    assert tree._gone(entry, timeout=0.1)


def test_the_current_run_ends_its_own_process_whatever_the_boot_reader_says(tmp_path, monkeypatch):
    """A record of this run was written in this boot -- main, and the worker, outlive no reboot -- so
    ``tree_gone`` needs no boot to act on its own live process."""
    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_boot_id", lambda: None)
    process = _spawn()
    try:
        launch(tmp_path, "node")
        record(tmp_path / "node.pid", process.pid)
        assert tree_gone(tmp_path, strict=True)
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize(
    ("raw", "boot"),
    [
        ("6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b\n", "6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"),
        ("6C1F9A2E-3B4D-4E5F-8A9B-0C1D2E3F4A5B", "6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"),
        ("", None),
        ("unknown", None),
        ("6c1f9a2e-3b4d-4e5f-8a9b", None),
    ],
)
def test_a_boot_reader_answer_is_an_identity_only_in_its_own_format(raw, boot):
    """Consilium r3, R3.2: validate the reader's output before taking it as a boot."""
    from backend.app.services import part_render_tree as tree

    assert tree._uuid_boot(raw) == boot


@pytest.mark.parametrize(
    ("value", "kind", "boot"), [(55, 4, "bootid-55"), ("55", 1, None), (-1, 4, None), (2**32, 4, None)]
)
def test_a_windows_boot_counter_is_a_dword(value, kind, boot):
    from backend.app.services import part_render_tree as tree

    assert tree._windows_boot(value, kind) == boot


class _Readable(_Holder):
    """A process whose command line carries the token, with the identity a platform reads (None: hidden)."""

    def __init__(self, pid, *, uid=None, user=None):
        super().__init__(pid)
        self.info["uids"] = None if uid is None else _Uids(uid, uid, uid)
        self.info["username"] = user


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize(
    ("owner", "readable"),
    [
        ("known", False),  # the holder's user cannot be read
        ("missing", True),  # the run's owner names no user
    ],
)
def test_a_public_token_is_no_authority_to_signal_an_unverified_user(monkeypatch, windows, owner, readable):
    """Consilium r4, R4.1: a token holder is signalled only when its user is READ and IS the run's owner."""
    ours = (_OWNER if windows else 1000) if owner == "known" else None
    holder = _Readable(9, uid=1000 if readable else None, user=(_OWNER if windows else None) if readable else None)
    if windows and readable:
        holder.info["username"] = "HOST\\me"
    _Holder.killed = []
    tree = _scan(monkeypatch, [holder], windows=windows)
    assert not tree._end_launched("a" * 32, 0.1, "HOST\\me" if windows and ours else ours)
    assert _Holder.killed == []


@pytest.mark.parametrize("windows", [False, True])
def test_a_token_holder_read_as_the_owner_is_ended(monkeypatch, windows):
    holder = _Readable(9, uid=1000, user="HOST\\me")
    _Holder.killed = []
    tree = _scan(monkeypatch, [], windows=windows)
    rounds = iter([[holder], []])  # killed in the first round, gone in the second
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: next(rounds))
    assert tree._end_launched("a" * 32, 0.1, "HOST\\me" if windows else 1000)
    assert _Holder.killed == [9]


@pytest.mark.parametrize("bad", ["unknown", "not-a-uuid", "bootid-no-counter", "bootid-4294967296", "BOOTID-1", " "])
def test_a_malformed_persisted_boot_is_no_proof_that_a_live_run_ended(tmp_path, bad):
    """Consilium r4, R4.2: a persisted boot counts only in its platform's format. Any other text is no
    observation of an earlier boot, and a run whose main still lives is not over."""
    from backend.app.services.part_render_tree import generation_gone

    generation, _ = _generation(tmp_path)
    record(generation / "owner.pid", os.getpid())  # this live process plays the earlier run's main
    data = json.loads((generation / "owner.pid").read_text(encoding="ascii"))
    data["boot"] = bad
    (generation / "owner.pid").write_text(json.dumps(data), encoding="ascii")
    assert not generation_gone(generation)


def test_a_valid_persisted_boot_of_another_platform_format_is_no_identity_here(monkeypatch):
    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_boot_id", lambda: _BOOT_A)
    other = "6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b" if sys.platform == "win32" else "bootid-41"
    assert not tree._another_boot({"boot": other})
    assert tree._another_boot({"boot": _BOOT_B})


@pytest.mark.parametrize(
    ("windows", "boot", "known"),
    [
        (True, "bootid-0", True),
        (True, "bootid-4294967295", True),
        (True, "bootid-4294967296", False),
        (True, "bootid-07", False),
        (True, "bootid-", False),
        (True, "6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", False),
        (False, "6c1f9a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", True),
        (False, "6C1F9A2E-3B4D-4E5F-8A9B-0C1D2E3F4A5B", False),
        (False, "bootid-41", False),
        (False, "unknown", False),
        (False, None, False),
        (False, 41, False),
    ],
)
def test_a_persisted_boot_counts_only_in_its_platform_format(monkeypatch, windows, boot, known):
    """Consilium r4, R4.2, both formats on every platform: exactly what the platform's reader writes."""
    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_WINDOWS_BOOT", windows)
    assert (tree._known_boot(boot) is not None) is known


class _Reaped:
    """A killed process another parent reaps while it is waited for: psutil 7's pidfd wait then raises the
    kernel's EINVAL, which it maps to nothing (``wait_pid_pidfd_open`` handles ESRCH only)."""

    def __init__(self, *, running: bool, zombie: bool = False):
        self.pid, self._running, self._zombie, self.killed = 4242, running, zombie, False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        import errno

        raise OSError(errno.EINVAL, "Invalid argument")

    def is_running(self):
        return self._running

    def status(self):
        import psutil

        return psutil.STATUS_ZOMBIE if self._zombie else psutil.STATUS_RUNNING


@pytest.mark.parametrize(
    ("running", "zombie", "ended"), [(False, False, True), (True, True, True), (True, False, False)]
)
def test_a_wait_the_kernel_answers_with_einval_asks_the_process_again(running, zombie, ended):
    """Reaped while waited for (or the pid now names a thread): its identity answers instead of the wait --
    gone or a stranger now, or a zombie, it has ended; still running as itself, it has not."""
    from backend.app.services import part_render_tree as tree

    assert tree._wait(_Reaped(running=running, zombie=zombie), timeout=0.1) is ended


def test_a_reaped_process_does_not_turn_the_proof_into_an_error(tmp_path, monkeypatch):
    """The record proof survives the race instead of raising out of it (which made the worker uncertain)."""
    from backend.app.services import part_render_tree as tree

    reaped = _Reaped(running=False)

    class Live:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 1000.0

        def status(self):
            import psutil

            return psutil.STATUS_RUNNING

        def kill(self):
            reaped.kill()

        def wait(self, timeout=None):
            return reaped.wait(timeout)

        def is_running(self):
            return reaped.is_running()

    monkeypatch.setattr(tree, "_BOOT_RELATIVE", False)
    monkeypatch.setattr(tree.psutil, "Process", Live)
    entry = tmp_path / "node.pid"
    entry.write_text(json.dumps({"pid": 4242, "create_time": 1000.0}), encoding="ascii")
    assert tree._gone(entry, timeout=0.1, this_run=True)
    assert reaped.killed


def test_a_setuid_process_of_our_user_is_not_ours_and_does_not_block_the_proof(monkeypatch):
    """Measured on CI's macOS runner (2026-10-09): a long-lived /usr/bin/sudo with real uid 501 -- the runner's
    user -- and effective uid 0, its command line unreadable. Read by its real uid alone it "may be ours", and
    every earlier run there stayed unproven. Our processes never change uid: real, effective and saved are all
    the owner's, so a process whose uids are not one is positively someone else's -- a sudo -s in a developer's
    terminal included."""
    sudo = _Unreadable(979, ppid=977)
    sudo.info["uids"] = _Uids(501, 0, 0)
    tree = _scan(monkeypatch, [sudo], windows=False)
    assert tree._end_launched("a" * 32, 0.1, 501)


def test_a_setuid_token_holder_is_a_stranger_and_never_signalled(monkeypatch):
    holder = _Readable(9, uid=501)
    holder.info["uids"] = _Uids(501, 0, 0)
    _Holder.killed = []
    tree = _scan(monkeypatch, [holder], windows=False)
    assert tree._end_launched("a" * 32, 0.1, 501)
    assert _Holder.killed == []


def test_a_record_of_a_process_with_mixed_uids_names_no_owner(monkeypatch):
    from backend.app.services import part_render_tree as tree

    class Setuid:
        def uids(self):
            return _Uids(501, 0, 0)

    monkeypatch.setattr(tree, "_WINDOWS", False)
    assert tree._identity(Setuid()) is None


# The token scan says why it proves nothing (spec part-render-unrecorded-launch §4.7). The walk of THIS run
# names the cause in the parent log; the answers are the ones _end_launched has always given.


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize(
    ("holder", "owner", "why"),
    [
        (lambda: _Readable(9), "known", "a holder's user unreadable"),
        (lambda: _Readable(9, uid=1000, user="HOST\\me"), "missing", "run owner names no user"),
        (lambda: _Unreadable(7, uid=1000, name="python.exe", ppid=6000), "known", "a possible holder unreadable"),
    ],
)
def test_a_scan_that_proves_nothing_says_why_and_signals_nobody(monkeypatch, windows, holder, owner, why):
    _Holder.killed = []
    tree = _scan(monkeypatch, [holder()], windows=windows)
    ours = ("HOST\\me" if windows else 1000) if owner == "known" else None
    assert tree._end_why("a" * 32, 0.1, ours) == (why, 0)
    assert _Holder.killed == []


@pytest.mark.parametrize("windows", [False, True])
def test_ending_counts_the_holders_it_saw_end(monkeypatch, windows):
    _Holder.killed = []
    tree = _scan(monkeypatch, [], windows=windows)
    rounds = iter([[_Readable(9, uid=1000, user="HOST\\me"), _Readable(10, uid=1000, user="HOST\\me")], []])
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: next(rounds))
    assert tree._end_why("a" * 32, 0.1, "HOST\\me" if windows else 1000) == (None, 2)
    assert _Holder.killed == [9, 10]


def test_ending_names_a_holder_that_outlived_its_kill(monkeypatch):
    tree = _scan(monkeypatch, [_Readable(9, uid=1000)], windows=False)
    monkeypatch.setattr(tree, "_wait", lambda _process, _timeout: False)
    why, ended = tree._end_why("a" * 32, 0.1, 1000)
    assert why.startswith("holder still running after its kill pid=9 status=") and ended == 0


def test_ending_names_a_holder_it_may_not_kill(monkeypatch):
    import psutil

    class Guarded(_Readable):
        def kill(self):
            raise psutil.AccessDenied(self.pid)

    tree = _scan(monkeypatch, [Guarded(9, uid=1000)], windows=False)
    assert tree._end_why("a" * 32, 0.1, 1000) == ("holder access denied pid=9", 0)


def test_a_holder_that_ended_by_itself_before_its_kill_is_no_refusal(monkeypatch):
    """Review focus 2: the child reads its dead guardian's EOF and exits between the scan and the kill."""
    import psutil

    class Exited(_Readable):
        def kill(self):
            raise psutil.NoSuchProcess(self.pid)

    tree = _scan(monkeypatch, [], windows=False)
    rounds = iter([[Exited(9, uid=1000)], []])
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: next(rounds))
    assert tree._end_why("a" * 32, 0.1, 1000) == (None, 0)


def test_ending_names_holders_that_keep_appearing(monkeypatch):
    tree = _scan(monkeypatch, [], windows=False)
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: [_Readable(9, uid=1000)])
    assert tree._end_why("a" * 32, 0.1, 1000) == ("holders still found after 4 rounds", 4)


@pytest.mark.parametrize("windows", [False, True])
def test_a_run_whose_owner_names_no_user_is_proven_when_no_holder_is_found(monkeypatch, windows):
    """What _end_launched has always allowed: no holder and no ambiguous candidate is the same success."""
    tree = _scan(monkeypatch, [], windows=windows)
    assert tree._end_why("a" * 32, 0.1, None) == (None, 0)


def test_a_stranger_holding_the_token_is_neither_signalled_nor_a_refusal(monkeypatch):
    _Holder.killed = []
    tree = _scan(monkeypatch, [_Readable(9, uid=4242)], windows=False)
    assert tree._end_why("a" * 32, 0.1, 1000) == (None, 0)
    assert _Holder.killed == []


@pytest.mark.parametrize(
    ("windows", "user", "owner"),
    [
        (False, 1000, 1000),
        (False, True, None),
        (False, "1000", None),
        (False, None, None),
        (True, "HOST\\me", "HOST\\me"),
        (True, 1000, None),
    ],
)
def test_the_run_owner_is_the_user_its_record_names_in_this_platforms_form(tmp_path, monkeypatch, windows, user, owner):
    from backend.app.services import part_render_tree as tree

    monkeypatch.setattr(tree, "_WINDOWS", windows)
    record_text = json.dumps({"pid": 1, "create_time": 0.0, "boot": None, "user": user})
    (tmp_path / "owner.pid").write_text(record_text, encoding="ascii")
    assert tree._run_owner(tmp_path) == owner


def test_a_run_without_a_readable_owner_record_names_no_user(tmp_path):
    """Review focus 3: no owner identity, no holder shown ours."""
    from backend.app.services import part_render_tree as tree

    assert tree._run_owner(tmp_path) is None
    (tmp_path / "owner.pid").write_text("{broken", encoding="ascii")
    assert tree._run_owner(tmp_path) is None
    (tmp_path / "owner.pid").write_text("[1000]", encoding="ascii")
    assert tree._run_owner(tmp_path) is None


# THIS run's strict proof ends a launch without its record (spec part-render-unrecorded-launch §4.1): once its
# parent is proven ended in the same walk, the holders of the parent's token -- a fork caught before its exec
# -- and then of the launch's own are killed and waited for until a scan finds none.


def _token_holder(token: str) -> subprocess.Popen:
    """A live process with a launch's token on its command line: the launched process itself, or a fork of its
    parent caught before it executed the child."""
    from backend.app.services.part_render_tree import launch_arg

    return subprocess.Popen([*SLEEP, launch_arg(token)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _this_run(tmp_path: Path) -> tuple[Path, Path]:
    """THIS run's staging and one attempt: its owner (this test process) and its worker, proven dead -- what
    ``retire`` has established before it walks an attempt (spec §4.2, R2)."""
    generation, attempt = _generation(tmp_path)
    record(generation / "owner.pid", os.getpid())
    for name in ("worker", "service"):
        launch(generation, name)
        _dead(generation / f"{name}.pid")
    return generation, attempt


def _stop(*processes: subprocess.Popen) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_this_runs_child_launched_without_its_record_is_ended_once_its_guardian_is(tmp_path):
    """The macOS spawn window of 2026-10-10 (spec §1): the worker died while its attempt's guardian was between
    starting part_render and recording it. The proof kills the recorded guardian, finds the child by its token
    and ends it, and the attempt is proven over (spec §4.1)."""
    generation, attempt = _this_run(tmp_path)
    guardian = _token_holder(launch(attempt, "guardian"))
    record(attempt / "guardian.pid", guardian.pid)
    child_token = launch(attempt, "child")
    child = _token_holder(child_token)
    notes: list[str] = []
    try:
        assert tree_unproven(attempt, strict=True, generation=generation, notes=notes) is None
        assert guardian.wait(timeout=5) is not None and child.wait(timeout=5) is not None
        assert _holders(child_token) == []
        assert len(notes) == 1
        assert notes[0].startswith("child launched without its record: ended by its token (holders=")
    finally:
        _stop(guardian, child)


@pytest.mark.skipif(os.name == "nt", reason="SIGSTOP holds a process before it reads EOF (POSIX)")
def test_a_stopped_child_launched_without_its_record_is_ended(tmp_path):
    """EOF is not an exit (consilium r2, R2.1): a child stopped before it read its dead guardian's EOF is ended."""
    import signal

    generation, attempt = _this_run(tmp_path)
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    child = _token_holder(launch(attempt, "child"))
    os.kill(child.pid, signal.SIGSTOP)
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) is None
        assert child.wait(timeout=5) is not None
    finally:
        _stop(child)


def test_a_fork_of_the_guardian_caught_before_its_exec_is_ended_before_the_child_is_looked_for(tmp_path, monkeypatch):
    """A guardian killed inside Popen may leave its fork still running the guardian's command line: its token's
    holders are ended first, so none can become a child after the child's scan (spec §4.1, step 3)."""
    from backend.app.services import part_render_tree as tree

    generation, attempt = _this_run(tmp_path)
    guardian_token = launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    fork = _token_holder(guardian_token)
    child_token = launch(attempt, "child")
    order: list[str] = []
    real = tree._end_why

    def spy(token, timeout, owner, confirmed=None):
        order.append(token)
        return real(token, timeout, owner, confirmed)

    monkeypatch.setattr(tree, "_end_why", spy)
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) is None
        assert fork.wait(timeout=5) is not None
        assert order == [guardian_token, child_token]
    finally:
        _stop(fork)


def test_a_child_with_a_half_written_record_is_ended_like_one_without(tmp_path):
    """The guardian died inside record(): child.pid.part beside child.launch. The launch precedes the spawn, so
    the token finds the child (spec §4.1)."""
    generation, attempt = _this_run(tmp_path)
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    child = _token_holder(launch(attempt, "child"))
    (attempt / "child.pid.part").write_text("{", encoding="ascii")
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) is None
        assert child.wait(timeout=5) is not None
    finally:
        _stop(child)


def test_a_guardian_launched_without_its_record_is_ended_once_its_worker_is_proven_dead(tmp_path):
    """The worker died between starting the attempt's guardian and recording it: the worker's own record is
    dead (R2), so the guardian is found by its token and ended."""
    generation, attempt = _this_run(tmp_path)
    guardian = _token_holder(launch(attempt, "guardian"))
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) is None
        assert guardian.wait(timeout=5) is not None
    finally:
        _stop(guardian)


def test_a_guardian_launched_without_its_record_under_a_worker_not_proven_dead_is_not_over(tmp_path, monkeypatch):
    """The worker may still start what the token names: nothing is signalled, and the proof says why."""
    import psutil

    generation, attempt = _generation(tmp_path)
    record(generation / "owner.pid", os.getpid())
    worker = _spawn()
    launch(generation, "service")
    record(generation / "service.pid", worker.pid)
    guardian = _token_holder(launch(attempt, "guardian"))
    real = psutil.Process

    def guarded(pid):
        if pid == worker.pid:
            raise psutil.AccessDenied(pid)
        return real(pid)

    monkeypatch.setattr(psutil, "Process", guarded)
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) == (
            f"guardian: launched without its record (parent service not proven ended: access denied pid={worker.pid})"
        )
        assert worker.poll() is None and guardian.poll() is None
    finally:
        monkeypatch.undo()
        _stop(worker, guardian)


def test_a_guardian_launched_without_its_record_under_an_unrecorded_worker_is_not_over(tmp_path):
    """A worker without its record never received its bootstrap: an attempt beside it is a state this code does
    not write, and the proof refuses it (spec §4.3; consilium: a parent missing while an attempt exists)."""
    generation, attempt = _generation(tmp_path)
    record(generation / "owner.pid", os.getpid())
    launch(generation, "service")  # launched, never recorded
    launch(attempt, "guardian")
    assert tree_unproven(attempt, strict=True, generation=generation) == (
        "guardian: launched without its record (parent service not proven ended: launched without its record)"
    )


def test_a_node_launched_without_its_record_is_ended_once_part_render_is(tmp_path):
    generation, attempt = _this_run(tmp_path)
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    child = _token_holder(launch(attempt, "child"))
    record(attempt / "child.pid", child.pid)
    node = _token_holder(launch(attempt, "node"))
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) is None
        assert child.wait(timeout=5) is not None and node.wait(timeout=5) is not None
    finally:
        _stop(child, node)


def _fake_run(tmp_path: Path, monkeypatch, processes, *, windows: bool, owner: bool = True):
    """This run's staging with a dead recorded guardian and a child launched with the test token "a" * 32, whose
    process table is ``processes`` (the scan's fakes)."""
    generation, attempt = _generation(tmp_path)
    user = ("HOST\\me" if windows else 1000) if owner else None
    (generation / "owner.pid").write_text(json.dumps({"user": user}), encoding="ascii")
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    (attempt / "child.launch").write_text("a" * 32, encoding="ascii")
    _Holder.killed = []
    tree = _scan(monkeypatch, processes, windows=windows)
    return tree, generation, attempt


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize(
    ("processes", "owner", "why"),
    [
        (lambda: [_Readable(9, uid=4242, user="OTHER\\mallory")], True, None),  # a stranger: not signalled, no refusal
        (lambda: [_Readable(9)], True, "a holder's user unreadable"),
        (lambda: [_Readable(9, uid=1000, user="HOST\\me")], False, "run owner names no user"),
        (lambda: [], False, None),  # no holder, no ambiguous candidate: the success _end_launched allows
        (
            lambda: [_Unreadable(7, uid=1000, name="python.exe", ppid=6000)],
            True,
            "parent guardian token: a possible holder unreadable",
        ),
    ],
)
def test_the_right_to_signal_in_this_run_is_the_one_earlier_runs_have(
    tmp_path, monkeypatch, windows, processes, owner, why
):
    """Spec §4.4 / consilium T7: the walk of this run signals and refuses exactly as _holders_why decides."""
    tree, generation, attempt = _fake_run(tmp_path, monkeypatch, processes(), windows=windows, owner=owner)
    expected = None if why is None else f"child: launched without its record ({why})"
    assert tree.tree_unproven(attempt, strict=True, generation=generation) == expected
    assert _Holder.killed == []


def test_a_holder_that_outlives_its_kill_keeps_the_attempt_unproven(tmp_path, monkeypatch):
    """Review focus 1: a holder in uninterruptible sleep on a hung share survives SIGKILL past the wait."""
    holder = _Readable(9, uid=1000, user="HOST\\me")
    tree, generation, attempt = _fake_run(tmp_path, monkeypatch, [holder], windows=False)
    monkeypatch.setattr(tree, "_wait", lambda _process, _timeout: False)
    why = tree.tree_unproven(attempt, strict=True, generation=generation)
    assert why.startswith("child: launched without its record (holder still running after its kill pid=9 status=")


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("token", "child: launched without its record (token unreadable)"),
        ("parent_never", "child: launched without its record (parent guardian never launched)"),
        ("parent_token", "child: launched without its record (parent guardian token unreadable)"),
        ("parent_record", "guardian: record unreadable"),
    ],
)
def test_a_launch_without_its_record_that_cannot_be_ended_says_why(tmp_path, state, why):
    generation, attempt = _this_run(tmp_path)
    if state != "parent_never":
        if state == "parent_token":
            (attempt / "guardian.launch").write_text("", encoding="ascii")  # review focus 5: its token is lost
        else:
            launch(attempt, "guardian")
        if state == "parent_record":
            (attempt / "guardian.pid").write_text("{broken", encoding="ascii")
        else:
            _dead(attempt / "guardian.pid")
    if state == "token":
        (attempt / "child.launch").write_text("", encoding="ascii")
    else:
        launch(attempt, "child")
    assert tree_unproven(attempt, strict=True, generation=generation) == why


@pytest.mark.parametrize("which", ["child", "guardian"])
def test_a_launch_token_that_is_not_ascii_is_unreadable_not_an_error(tmp_path, which):
    """Consilium UL-R1: today's strict proof never reads a launch file; this one does. A damaged one -- bytes
    that are not ASCII -- is no token: a refusal with its reason, never an exception that leaves the proof's
    caller without an answer; nothing is signalled."""
    generation, attempt = _this_run(tmp_path)
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    child = _token_holder(launch(attempt, "child"))
    (attempt / f"{which}.launch").write_bytes(b"\xff")
    why = "token unreadable" if which == "child" else "parent guardian token unreadable"
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) == (
            f"child: launched without its record ({why})"
        )
        assert child.poll() is None
    finally:
        _stop(child)


def test_a_fully_recorded_attempt_of_this_run_scans_no_process_table(tmp_path, monkeypatch):
    """Spec §4.5 / T8 (a): what is proven by records today is proven by records alone -- no scan, same answer."""
    import psutil

    generation, attempt = _this_run(tmp_path)
    for name in ("guardian", "child", "node"):
        launch(attempt, name)
        _dead(attempt / f"{name}.pid")

    def no_scan(attrs):
        raise AssertionError("a fully recorded attempt scans no process table")

    monkeypatch.setattr(psutil, "process_iter", no_scan)
    assert tree_unproven(attempt, strict=True, generation=generation) is None


@pytest.mark.parametrize(
    ("state", "why"),
    [("unreadable", "guardian: record unreadable"), ("half_written", "guardian: half-written record")],
)
def test_a_refusal_the_rule_does_not_touch_keeps_its_reason_in_this_run(tmp_path, state, why):
    """T8 (a): a refusal outside §4.1 is the same refusal with the run's staging given."""
    generation, attempt = _this_run(tmp_path)
    if state == "unreadable":
        (attempt / "guardian.pid").write_text("{broken", encoding="ascii")
    else:
        (attempt / "guardian.pid.part").write_text("{", encoding="ascii")  # no launch: a state this code never writes
    assert tree_unproven(attempt, strict=True, generation=generation) == why


def test_the_workers_own_proof_still_leaves_a_launch_without_its_record_alone(tmp_path):
    """T8 (a): not strict -- the worker right after stop() killed the attempt's group / Job Object -- no scan,
    no signal, as before."""
    generation, attempt = _this_run(tmp_path)
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    child = _token_holder(launch(attempt, "child"))
    try:
        assert tree_unproven(attempt, strict=False) is None
        assert child.poll() is None
    finally:
        _stop(child)


# R2 (spec part-render-unrecorded-launch §4.2): after stop(), the worker that may have launched attempts is
# positively ended by its records -- the parent every attempt guardian's launch needs proven.


def test_this_runs_worker_records_are_proven_by_their_records(tmp_path):
    from backend.app.services.part_render_tree import worker_records_unproven

    generation, _ = _generation(tmp_path)
    launch(generation, "worker")
    _dead(generation / "worker.pid")
    worker = _spawn()
    launch(generation, "service")
    record(generation / "service.pid", worker.pid)
    try:
        assert worker_records_unproven(generation) is None
        assert worker.wait(timeout=5) is not None  # it outlived stop(): killed by its record
    finally:
        _stop(worker)


def test_a_worker_record_that_cannot_be_read_is_unproven(tmp_path):
    from backend.app.services.part_render_tree import worker_records_unproven

    generation, _ = _generation(tmp_path)
    (generation / "service.pid").write_text("{broken", encoding="ascii")
    assert worker_records_unproven(generation) == "service: record unreadable"


def test_a_run_whose_worker_never_started_has_nothing_to_prove(tmp_path):
    """Consilium Q1: a broker unavailable before the first spawn leaves no worker record, and that absence
    alone is no refusal (spec §4.3)."""
    from backend.app.services.part_render_tree import worker_records_unproven

    generation, _ = _generation(tmp_path)
    assert worker_records_unproven(generation) is None


def test_a_worker_launched_without_its_record_is_a_bounded_limit_not_a_proof(tmp_path):
    """Consilium Q2: a worker without its record never received its bootstrap and launched no attempt. The
    worker proof does not look at it -- and claims nothing about its end: EOF is no proof (spec §4.2). An
    attempt beside it is refused (``…_under_an_unrecorded_worker_is_not_over``)."""
    from backend.app.services.part_render_tree import worker_records_unproven

    generation, _ = _generation(tmp_path)
    launch(generation, "service")
    assert worker_records_unproven(generation) is None


# Final review of the unrecorded-launch change (fix pass): M1-M3.


def test_a_refusal_after_ending_holders_says_how_many_it_ended(tmp_path, monkeypatch):
    """Review M1: a launch refused after some of its token holders were already killed says so in the parent
    log -- the kills happened, whatever the refusal."""
    from backend.app.services.part_render_tree import launch_arg

    generation, attempt = _generation(tmp_path)
    (generation / "owner.pid").write_text(json.dumps({"user": 1000}), encoding="ascii")
    (attempt / "guardian.launch").write_text("b" * 32, encoding="ascii")
    _dead(attempt / "guardian.pid")
    (attempt / "child.launch").write_text("a" * 32, encoding="ascii")
    fork = _Readable(9, uid=1000)
    fork.info["cmdline"] = ["python", launch_arg("b" * 32)]  # the guardian's fork, caught before its exec
    _Holder.killed = []
    tree = _scan(monkeypatch, [], windows=False)
    rounds = iter([[fork], [], [_Unreadable(7, uid=1000)]])  # parent token: one holder, then none; then the child's
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: next(rounds))
    notes: list[str] = []
    why = tree.tree_unproven(attempt, strict=True, generation=generation, notes=notes)
    assert why == "child: launched without its record (a possible holder unreadable)"
    assert _Holder.killed == [9]
    assert notes == ["child launched without its record: 1 token holders ended before its refusal"]


def test_a_guardian_launched_without_its_record_has_its_workers_fork_ended_first(tmp_path):
    """Review M2: the guardian's parent is the worker, so the worker's own token is scanned first -- a fork of the
    worker caught before it executed the guardian still runs the worker's command line (spec §4.1, step 3)."""
    generation, attempt = _this_run(tmp_path)
    fork = _token_holder((generation / "service.launch").read_text(encoding="ascii"))
    launch(attempt, "guardian")
    try:
        assert tree_unproven(attempt, strict=True, generation=generation) is None
        assert fork.wait(timeout=5) is not None
    finally:
        _stop(fork)


def test_this_runs_worker_guardian_record_is_proven_too(tmp_path):
    """Review M2: R2 proves both of the generation's records -- the worker guardian as well as the worker."""
    from backend.app.services.part_render_tree import worker_records_unproven

    generation, _ = _generation(tmp_path)
    guardian = _spawn()
    launch(generation, "worker")
    record(generation / "worker.pid", guardian.pid)
    launch(generation, "service")
    _dead(generation / "service.pid")
    try:
        assert worker_records_unproven(generation) is None
        assert guardian.wait(timeout=5) is not None
    finally:
        _stop(guardian)


def test_a_strict_proof_without_this_runs_staging_refuses_a_launch_without_its_record_as_before(tmp_path, monkeypatch):
    """Review M3: the rule needs this run's staging -- its owner names the user a holder must be. Without it the
    strict proof refuses exactly as before the rule, and scans nothing."""
    import psutil

    _, attempt = _this_run(tmp_path)
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
    launch(attempt, "child")

    def no_scan(attrs):
        raise AssertionError("no staging, no scan")

    monkeypatch.setattr(psutil, "process_iter", no_scan)
    assert tree_unproven(attempt, strict=True) == "child: launched without its record"
