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
