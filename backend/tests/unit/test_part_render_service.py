"""part_render_service: an attempt is answered only once its whole tree is proven gone (plan E3, task 17)."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from backend.app import part_render_service
from backend.app.part_render_service import Service
from backend.app.services.part_render_protocol import unpack
from backend.app.services.preview_process import PreviewProcess
from backend.tests.fixtures.part_render_3mf import gcode, two_objects_3mf, write_3mf
from backend.tests.unit.services.test_part_render_node import NODE, needs_node

ROOT = Path(__file__).resolve().parents[3]
GEN, EPOCH, ATTEMPT = "a" * 32, "b" * 32, "c" * 32
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO is how a read is hung here (POSIX)")


class FakeStore:
    """The two calls analysis_transport.put makes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def get_info(self, key):
        from nats.js.errors import ObjectNotFoundError

        raise ObjectNotFoundError

    async def put(self, key, reader, meta=None):
        data, buffer = bytearray(), bytearray(65536)
        while count := reader.readinto(buffer):
            data += buffer[:count]
        self.objects[key] = bytes(data)


def make_service(tmp_path: Path) -> Service:
    (tmp_path / "service").mkdir()
    service = Service(
        {"generation": GEN, "epoch": EPOCH, "staging": str(tmp_path / "service"), "url": "", "token": "", "bucket": ""}
    )
    service.store = FakeStore()
    return service


def command(task, *, node=None, sequence=1, deadline_s=60.0, operation="run") -> dict:
    return {
        "version": 1,
        "generation": GEN,
        "epoch": EPOCH,
        "attempt_id": ATTEMPT,
        "sequence": sequence,
        "operation": operation,
        "deadline_ns": time.monotonic_ns() + int(deadline_s * 1e9),
        "task": task,
        "node": node,
    }


def task_for(source: Path, *, plate: int = 1, kind: str = "3mf") -> dict:
    data = source.read_bytes()
    return {
        "path": str(source),
        "root": str(source.parent),
        "kind": kind,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "plate_index": plate,
    }


def hung_task(tmp_path: Path) -> dict:
    """A source whose open never returns -- what a hung NAS read is to the child."""
    fifo = tmp_path / "hung.3mf"
    os.mkfifo(fifo)
    return {"path": str(fifo), "root": str(tmp_path), "kind": "3mf", "sha256": "0" * 64, "size": 1, "plate_index": 1}


def big_plate(path: Path, lines: int = 3_000_000) -> Path:
    """Enough G-code that Node is still busy seconds after it starts."""
    body = b"".join(b"G1 X%d Y%d E0.1\n" % (i % 200, (i // 200) % 200) for i in range(lines))
    return write_3mf(
        path,
        {1: gcode("two-objects") + b"; start printing object, unique label id: 101\n" + body},
        objects={1: {101: "A", 202: "B"}},
    )


async def tree_of(service: Service, *, at_least: int = 1) -> list[psutil.Process]:
    """The running attempt's guardian and descendants, once there are ``at_least`` descendants."""
    for _ in range(800):
        child = service.child
        if child is not None:
            try:
                guardian = psutil.Process(child.process.pid)
                descendants = guardian.children(recursive=True)
                if len(descendants) >= at_least:
                    return [guardian, *descendants]
            except psutil.NoSuchProcess:
                pass
        await asyncio.sleep(0.025)
    raise AssertionError("the attempt never started")


def gone(processes: list[psutil.Process]) -> bool:
    _, alive = psutil.wait_procs(processes, timeout=10)
    return not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]


def dead_now(processes: list[psutil.Process]) -> bool:
    def dead(process):
        try:
            return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    return all(dead(p) for p in processes)


@pytest.mark.asyncio
async def test_an_attempt_without_node_answers_done_with_its_packed_result(tmp_path):
    service = make_service(tmp_path)
    reply = await service.command(command(task_for(two_objects_3mf(tmp_path / "f.3mf"))))
    assert reply["outcome"] == "done" and reply["result"]["outcome"] == "ok"
    assert reply["artifact"]["key"] == f"{ATTEMPT}_partrender"
    (tmp_path / "r.bin").write_bytes(service.store.objects[reply["artifact"]["key"]])
    (tmp_path / "out").mkdir()
    assert "manifest.json" in unpack(tmp_path / "r.bin", tmp_path / "out")
    assert not (tmp_path / "service" / ATTEMPT).exists()


@pytest.mark.asyncio
async def test_a_result_of_a_file_is_done_and_carries_no_artifact(tmp_path):
    service = make_service(tmp_path)
    reply = await service.command(command(task_for(two_objects_3mf(tmp_path / "f.3mf"), plate=7)))
    assert reply == {"outcome": "done", "result": {"outcome": "unavailable", "reason": "no_gcode"}, "artifact": None}


@needs_fifo
@pytest.mark.asyncio
async def test_a_hung_source_read_ends_only_with_the_tree_proven_gone(tmp_path):
    service = make_service(tmp_path)
    started = time.monotonic()
    run = asyncio.create_task(service.command(command(hung_task(tmp_path), deadline_s=2)))
    processes = await tree_of(service)
    # the worker itself never touches the source, so it answers while the child hangs
    status = await asyncio.wait_for(service.command(command(None, operation="status", sequence=2)), 1)
    assert status["state"] == "busy"
    reply = await run
    assert reply == {"outcome": "timeout"}
    assert dead_now(processes)  # gone at the moment of the answer, not some time after it
    assert time.monotonic() - started < 2 + 25
    assert not (tmp_path / "service" / ATTEMPT).exists()


@needs_fifo
@pytest.mark.asyncio
async def test_cancel_during_a_hung_read_answers_only_after_the_tree_is_gone(tmp_path):
    service = make_service(tmp_path)
    run_command = command(hung_task(tmp_path), deadline_s=120)
    run = asyncio.create_task(service.command(run_command))
    processes = await tree_of(service)
    cancel = await service.command({**run_command, "operation": "cancel"})
    assert cancel == {"outcome": "canceled"}
    assert dead_now(processes)
    assert (await run)["outcome"] == "canceled"


@pytest.mark.asyncio
async def test_an_unproven_tree_refuses_every_later_run_and_keeps_its_staging(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    async def unproven(child, root):
        await part_render_service.disk(child.stop)  # really end it, then report the proof as failed
        raise part_render_service.PreviewError("unavailable")

    monkeypatch.setattr(service, "_end_tree", unproven)
    source = two_objects_3mf(tmp_path / "f.3mf")
    assert await service.command(command(task_for(source))) == {"outcome": "unavailable"}
    spawned = []
    monkeypatch.setattr(part_render_service, "PreviewProcess", lambda *args, **kwargs: spawned.append(args))
    assert await service.command(command(task_for(source), sequence=2)) == {"outcome": "unavailable"}
    assert spawned == []  # no attempt starts beside one whose tree is not proven gone
    assert (tmp_path / "service" / ATTEMPT).exists()  # left for whoever proves its owners gone


SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


class FailingStart:
    """A PreviewProcess whose start fails after the process exists, and whose cleanup cannot prove it gone."""

    started: list = []

    tokens: list = []

    def __init__(self, module, bootstrap, cache, *, on_spawn=None, launch_token=None):
        FailingStart.tokens.append(launch_token)
        process = subprocess.Popen(SLEEP, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        FailingStart.started.append(process)
        on_spawn(process.pid)
        raise part_render_service.SpawnUnproven(process.pid)


@pytest.mark.asyncio
async def test_a_failed_start_whose_cleanup_is_unproven_keeps_the_proof(tmp_path, monkeypatch):
    """Consilium E3.2-R1: not crashed, not cleaned -- unavailable, uncertain, the records kept for main."""
    monkeypatch.setattr(part_render_service, "PreviewProcess", FailingStart)
    service = make_service(tmp_path)
    try:
        assert await service.command(command(task_for(two_objects_3mf(tmp_path / "f.3mf")))) == {
            "outcome": "unavailable"
        }
        assert service.uncertain
        attempt = tmp_path / "service" / ATTEMPT
        assert (attempt / "guardian.launch").exists() and (attempt / "guardian.pid").exists()
        assert await service.command(command(task_for(tmp_path / "f.3mf"), sequence=2)) == {"outcome": "unavailable"}
        assert len(FailingStart.started) == 1  # no second attempt beside the unproven one
        # the guardian is started with the token of its launch (consilium r2, R2.1)
        assert FailingStart.tokens == [(attempt / "guardian.launch").read_text(encoding="ascii")]
    finally:
        for process in FailingStart.started:
            process.kill()
            process.wait()
        FailingStart.started.clear()
        FailingStart.tokens.clear()


@pytest.mark.asyncio
async def test_a_start_that_fails_before_any_process_exists_is_a_crash(tmp_path, monkeypatch):
    def no_python(module, bootstrap, cache, *, on_spawn=None, launch_token=None):
        raise FileNotFoundError("the interpreter is gone")  # Popen itself failed: there is no process

    monkeypatch.setattr(part_render_service, "PreviewProcess", no_python)
    service = make_service(tmp_path)
    assert await service.command(command(task_for(two_objects_3mf(tmp_path / "f.3mf")))) == {"outcome": "crashed"}
    assert not service.uncertain
    assert not (tmp_path / "service" / ATTEMPT).exists()


@pytest.mark.asyncio
async def test_a_cancel_never_turns_an_unproven_end_into_a_proven_one(tmp_path, monkeypatch):
    """Consilium E3-R2: the run's answer was lost; its cached unavailable must not come back as canceled."""
    service = make_service(tmp_path)

    async def unproven(child, root):
        await part_render_service.disk(child.stop)
        raise part_render_service.PreviewError("unavailable")

    monkeypatch.setattr(service, "_end_tree", unproven)
    run_command = command(task_for(two_objects_3mf(tmp_path / "f.3mf")))
    assert await service.command(run_command) == {"outcome": "unavailable"}  # imagine main never saw this
    assert await service.command({**run_command, "operation": "cancel"}) == {"outcome": "unavailable"}
    later = command(task_for(tmp_path / "f.3mf"), sequence=7, operation="cancel")
    assert await service.command(later) == {"outcome": "unavailable"}  # a late cancel, too


OWNER_DIES_BEFORE_THE_RECORD = """
import json, sys, time
from pathlib import Path
from backend.app.services.part_render_tree import launch
from backend.app.services.preview_process import PreviewProcess
boot = json.loads(sys.argv[1])
root = Path(boot["root"])
launch(root, "guardian")

def stall(pid):
    print(pid, flush=True)
    time.sleep(120)  # killed here: the guardian exists, its record does not

PreviewProcess("backend.app.part_render", boot, root.parent / "cache", on_spawn=stall)
"""


def test_a_worker_dying_between_spawn_and_record_leaves_no_proof(tmp_path):
    """Consilium E3-R1: the strict proof refuses; the guardian, never bootstrapped, leaves by EOF."""
    from backend.app.services.part_render_tree import tree_gone

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    boot = {"root": str(attempt), "task": {}, "deadline_ns": 0, "node": None}  # never delivered: the owner dies first
    owner = subprocess.Popen(
        [sys.executable, "-c", OWNER_DIES_BEFORE_THE_RECORD, json.dumps(boot)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        guardian = psutil.Process(int(owner.stdout.readline()))
        owner.kill()
        owner.wait(timeout=5)
        assert (attempt / "guardian.launch").exists() and not (attempt / "guardian.pid").exists()
        assert not tree_gone(attempt, strict=True)  # main would close admission and keep this directory
        assert gone([guardian])  # with no bootstrap, EOF ends it
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@needs_node
@pytest.mark.asyncio
async def test_the_watchdog_kills_a_busy_node_and_proves_it_gone(tmp_path, monkeypatch):
    service_root = tmp_path / "service"

    def rss(self):  # over budget the moment Node is recorded, so the kill lands on a live Node
        return 10**12 if next(service_root.glob("*/node.pid"), None) else 0

    monkeypatch.setattr(PreviewProcess, "rss", rss)
    service = make_service(tmp_path)
    run = asyncio.create_task(
        service.command(command(task_for(big_plate(tmp_path / "f.3mf")), node=NODE, deadline_s=120))
    )
    processes = await tree_of(service, at_least=2)  # part_render and Node
    reply = await run
    assert reply == {"outcome": "memory_limit"}
    assert dead_now(processes)


@needs_node
@pytest.mark.asyncio
async def test_a_killed_child_is_crashed_and_leaves_no_node(tmp_path):
    import psutil as ps

    service = make_service(tmp_path)
    run = asyncio.create_task(
        service.command(command(task_for(big_plate(tmp_path / "f.3mf")), node=NODE, deadline_s=120))
    )
    processes = await tree_of(service, at_least=2)
    attempt = tmp_path / "service" / ATTEMPT
    for _ in range(400):
        if (attempt / "node.pid").exists():
            break
        await asyncio.sleep(0.025)
    child_pid = json.loads((attempt / "child.pid").read_text(encoding="ascii"))["pid"]
    ps.Process(child_pid).kill()  # part_render dies under its Node
    assert await run == {"outcome": "crashed"}
    assert dead_now(processes)  # Node included: the records and the group kill both reach it


@needs_node
@pytest.mark.asyncio
async def test_a_node_past_the_childs_deadline_is_a_timeout_of_the_file(tmp_path):
    service = make_service(tmp_path)
    reply = await service.command(command(task_for(big_plate(tmp_path / "f.3mf")), node=NODE, deadline_s=7))
    assert reply == {"outcome": "done", "result": {"outcome": "failed", "reason": "timeout"}, "artifact": None}


OWNER = """
import json, sys, time
from pathlib import Path
from backend.app.services.preview_process import PreviewProcess
boot = json.loads(sys.argv[1])
child = PreviewProcess("backend.app.part_render", boot, Path(boot["root"]).parent / "cache")
print(child.process.pid, flush=True)
time.sleep(120)
"""


def _owner_dies(tmp_path: Path, task: dict, *, node, at_least: int) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    boot = {"root": str(attempt), "task": task, "deadline_ns": time.monotonic_ns() + 120 * 10**9, "node": node}
    owner = subprocess.Popen(
        [sys.executable, "-c", OWNER, json.dumps(boot)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        guardian = psutil.Process(int(owner.stdout.readline()))
        for _ in range(800):
            descendants = guardian.children(recursive=True)
            if len(descendants) >= at_least:
                break
            time.sleep(0.025)
        assert len(descendants) >= at_least
        owner.kill()
        owner.wait(timeout=5)
        assert gone([guardian, *descendants])
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@needs_fifo
def test_the_owner_dying_takes_a_hung_reader_with_it(tmp_path):
    _owner_dies(tmp_path, hung_task(tmp_path), node=None, at_least=1)


@needs_node
def test_the_owner_dying_takes_a_busy_node_with_it(tmp_path):
    _owner_dies(tmp_path, task_for(big_plate(tmp_path / "f.3mf")), node=NODE, at_least=2)


@pytest.mark.asyncio
async def test_a_cancel_during_the_upload_is_answered_and_leaves_no_staging(tmp_path):
    """Final review M5: the tree is proven gone before the upload starts; a cancel then gets an answer instead
    of a lost reply that would make main retire a healthy worker."""
    service = make_service(tmp_path)
    started = asyncio.Event()

    class Blocking(FakeStore):
        async def put(self, key, reader, meta=None):
            started.set()
            await asyncio.Event().wait()

    service.store = Blocking()
    run_command = command(task_for(two_objects_3mf(tmp_path / "f.3mf")))
    run = asyncio.create_task(service.command(run_command))
    await asyncio.wait_for(started.wait(), 60)
    assert await service.command({**run_command, "operation": "cancel"}) == {"outcome": "canceled"}
    assert (await run)["outcome"] == "canceled"
    assert not (tmp_path / "service" / ATTEMPT).exists()
