"""part_render_tree: an attempt's processes are recorded by their parents and proven gone (plan E3, R12)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.services.part_render_tree import launch, record, tree_gone

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


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
    monkeypatch.setattr(tree, "_boot_id", lambda: "boot-a")  # the same boot: only the clock moved
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


def test_a_live_recorded_process_of_an_earlier_generation_is_killed_not_waved_through(tmp_path):
    """Consilium E3-I-R1: valid positive evidence of a live process is acted on, whatever any scan says."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    _dead(generation / "owner.pid")
    alive = _spawn()
    try:
        launch(attempt, "node")
        record(attempt / "node.pid", alive.pid)
        assert generation_gone(generation)
        assert alive.wait(timeout=5) is not None  # killed by the proof
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
    entry = tmp_path / "node.pid"
    entry.write_text(json.dumps({"pid": 4242, "create_time": 1000.0}), encoding="ascii")
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


def test_an_unrecorded_launch_under_a_dead_parent_is_found_by_its_token_and_ended(tmp_path):
    """Consilium r2, R2.1: a dead parent proves the input was withheld, not that the child exited. The child
    is found by the token its launch carries -- no new one can be born once the parent is dead -- and ended."""
    from backend.app.services.part_render_tree import generation_gone

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    unrecorded = _launched_by_a_dead_parent(attempt, "node")
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
def test_a_stopped_real_node_launched_without_a_record_is_ended_by_the_proof(tmp_path):
    """Consilium r2, R2.1 with the pinned Node and the production command: stopped before it could read EOF,
    its parent dead -- the proof ends it instead of trusting the EOF."""
    import signal

    import psutil

    from backend.app.services.part_render_tree import generation_gone
    from backend.tests.unit.services.test_part_render_node import NODE

    if NODE is None:
        pytest.skip("no pinned Node")
    generation, attempt = _generation(tmp_path)
    for name in ("owner", "worker", "service"):
        _dead(generation / f"{name}.pid")
    launch(attempt, "guardian")
    _dead(attempt / "guardian.pid")
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
    parent = subprocess.Popen([sys.executable, "-c", code, str(attempt), str(NODE)], stdout=subprocess.PIPE)
    node_pid = int(parent.stdout.readline())
    parent.wait(timeout=10)
    try:
        assert psutil.Process(node_pid).status() == psutil.STATUS_STOPPED
        assert generation_gone(generation)
        assert not psutil.pid_exists(node_pid) or psutil.Process(node_pid).status() == psutil.STATUS_ZOMBIE
    finally:
        try:
            psutil.Process(node_pid).kill()
        except psutil.NoSuchProcess:
            pass


class _Kernel:
    """A modelled OS: the boot the reader reports, and whether anything was signalled."""

    boot = "boot-a"
    killed = False


def _model(monkeypatch, *, ticks: float = 25.0):
    import psutil

    from backend.app.services import part_render_tree as tree

    _Kernel.boot, _Kernel.killed = "boot-a", False

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
    _Kernel.boot = "boot-b"
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

    monkeypatch.setattr(tree, "_boot_id", lambda: "boot-a")
    generation, attempt = _generation(tmp_path)
    stranger = _spawn()  # what holds the recorded pids in the next boot
    try:
        record(generation / "owner.pid", stranger.pid)
        record(generation / "worker.pid", stranger.pid)
        (attempt / "node.launch").write_text("", encoding="ascii")  # its token lost
        (attempt / "child.pid.part").write_text("{", encoding="ascii")  # its record torn
        monkeypatch.setattr(tree, "_boot_id", lambda: "boot-b")
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


def test_a_fork_caught_before_it_executed_its_child_is_ended_by_its_parents_token(tmp_path):
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
        self.info = {"cmdline": ["python", launch_arg("a" * 32)]}

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
    assert tree._end_launched("a" * 32, timeout=0.1)
    assert _Holder.killed == [1, 2]


def test_holders_that_keep_appearing_are_not_proven_ended(monkeypatch):
    from backend.app.services import part_render_tree as tree

    _Holder.killed = []
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: [_Holder(len(_Holder.killed) + 1)])
    assert not tree._end_launched("a" * 32, timeout=0.1)


class _Stranger:
    """A process of another local user started with a token it read off our command line (argv is public)."""

    touched = False

    def __init__(self, token: str):
        from backend.app.services.part_render_tree import launch_arg

        self.pid = 4242
        self.info = {"cmdline": ["sleep", launch_arg(token)], "username": "mallory"}

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


def test_a_run_whose_owner_record_names_no_user_counts_every_holder(tmp_path, monkeypatch):
    """A record from before the user was stored cannot tell a stranger from ours: fail closed."""
    from backend.app.services import part_render_tree as tree

    generation, attempt = _generation(tmp_path)
    _dead_ancestors(generation, attempt)
    owner = json.loads((generation / "owner.pid").read_text(encoding="ascii"))
    owner.pop("user", None)
    (generation / "owner.pid").write_text(json.dumps(owner), encoding="ascii")
    token = launch(attempt, "node")
    monkeypatch.setattr(tree.psutil, "process_iter", lambda attrs: [_Stranger(token)])
    assert not tree.generation_gone(generation)
