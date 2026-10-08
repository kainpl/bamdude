"""part_render_tree: an attempt's processes are recorded by their parents and proven gone (plan E3, R12)."""

import json
import subprocess
import sys

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


def test_strays_names_a_process_of_ours_outside_this_tree_and_not_our_own_children(tmp_path):
    """Final review C2: what an earlier run could have left alive is found by what it runs."""
    import os

    from backend.app.services.part_render_tree import strays

    marker = [sys.executable, "-c", "import time; time.sleep(60)", "backend.app.part_render"]
    ours = subprocess.Popen(marker, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    other = subprocess.Popen(SLEEP, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert ours.pid not in strays()  # our own child is no stray
        assert ours.pid in strays(own=other.pid)  # seen from a process it does not descend from
        assert other.pid not in strays(own=other.pid)
    finally:
        for process in (ours, other):
            process.kill()
            process.wait()


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
