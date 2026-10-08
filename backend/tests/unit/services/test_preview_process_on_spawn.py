"""PreviewProcess(on_spawn=...): the owner records the guardian before the guardian has any work (plan E3, R12)."""

import time

import psutil
import pytest

from backend.app.services.preview_process import PreviewProcess


def _work(process: psutil.Process) -> bool:
    """A process the guardian started for its work. Not its console host, and not the interpreter a Windows
    venv launcher starts in its place (the launcher waits for it and takes it down with it)."""
    try:
        return process.name().lower() != "conhost.exe" and "backend.app.worker_guardian" not in process.cmdline()
    except psutil.Error:
        return False


def test_on_spawn_runs_before_the_bootstrap(tmp_path):
    seen: dict = {}

    def note(pid: int) -> None:
        seen["pid"] = pid
        time.sleep(0.3)  # the guardian waits for its bootstrap: it has spawned nothing yet
        seen["children"] = [p for p in psutil.Process(pid).children(recursive=True) if _work(p)]

    boot = {"root": str(tmp_path), "operation": "mesh", "kind": "stl", "deadline": time.monotonic_ns()}
    child = PreviewProcess("backend.app.preview_render", boot, tmp_path / "cache", on_spawn=note)
    try:
        assert seen["pid"] == child.process.pid
        assert seen["children"] == []
    finally:
        child.stop()


def test_a_failing_on_spawn_stops_the_guardian(tmp_path):
    pids: list[int] = []

    def refuse(pid: int) -> None:
        pids.append(pid)
        raise OSError("cannot record")

    with pytest.raises(OSError):
        PreviewProcess("backend.app.preview_render", {"root": str(tmp_path)}, tmp_path / "cache", on_spawn=refuse)
    _, alive = psutil.wait_procs([psutil.Process(pids[0])] if psutil.pid_exists(pids[0]) else [], timeout=5)
    assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]


def test_a_failed_start_whose_cleanup_is_unproven_raises_spawn_unproven(tmp_path, monkeypatch):
    """Consilium E3.2-R1: the caller learns the process existed and is not proven gone."""
    from backend.app.services.preview_process import SpawnUnproven
    from backend.app.services.preview_protocol import PreviewError

    def unproven(self):
        raise PreviewError("unavailable")

    monkeypatch.setattr(PreviewProcess, "stop", unproven)
    pids: list[int] = []

    def refuse(pid: int) -> None:
        pids.append(pid)
        raise OSError("cannot record")

    try:
        with pytest.raises(SpawnUnproven) as err:
            PreviewProcess("backend.app.preview_render", {"root": str(tmp_path)}, tmp_path / "cache", on_spawn=refuse)
        assert err.value.pid == pids[0]
        assert isinstance(err.value, PreviewError) and err.value.outcome == "unavailable"  # every old caller agrees
    finally:
        guardian = psutil.Process(pids[0])
        guardian.kill()
        guardian.wait(timeout=5)
