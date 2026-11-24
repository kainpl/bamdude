"""STEP ownership on both sides of the preview service (plan A3): a converter that is not proven over starts no PNG
child and makes the service uncertain; a cancel during the PNG child's spawn still owns and stops that child; on
the real chain a cancelled or preempted converter is reaped before the slot is released, no further child follows,
and four timeouts or four preemptions in a row keep the same service. A bundle that vanishes between main's gate
and the service's own check is still only cad_unavailable."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from backend.app import step_runtime
from backend.app.services.preview_protocol import C_CANCEL_SECONDS, Command
from backend.app.services.preview_runtime import PreviewRuntime

ROOT = Path(__file__).resolve().parents[4]
BOX = ROOT / "backend" / "tests" / "fixtures" / "step" / "authored" / "box_mm.step"
GEN, EPOCH = "g" * 32, "e" * 32
CONVERTER = "backend.app.services.step_thumbnail"


def _step_command(attempt: str, seq: int = 1, *, seconds: float = 60) -> Command:
    return Command.parse(
        {
            "protocol_version": 2,
            "app_generation": GEN,
            "service_epoch": EPOCH,
            "attempt_id": attempt,
            "attempt_seq": seq,
            "operation": "run",
            "deadline_monotonic_ns": time.monotonic_ns() + int(seconds * 1e9),
            "manifest": [{"role": "mesh", "key": f"{attempt}_mesh", "kind": "step", "size": 1, "digest": "b" * 64}],
            "cad": {"manifest_digest": "c" * 64, "bundle_key": "win-x64-cp312"},
        },
        GEN,
        EPOCH,
    )


def _service(tmp_path, monkeypatch, child_factory):
    """An in-process Service whose transfer is faked and whose children are ``child_factory``."""
    import backend.app.preview_service as module

    class _NC:
        def jetstream(self, timeout):
            class _JS:
                async def object_store(inner, bucket):
                    return None

            return _JS()

    async def connect(*args, **kwargs):
        return _NC()

    async def no_close(nc):
        return None

    async def fake_get(store, ref, path, deadline):
        path.write_bytes(b"ISO-10303-21;")

    monkeypatch.setattr(module, "nats", SimpleNamespace(connect=connect))
    monkeypatch.setattr(module, "close_transfer", no_close)
    monkeypatch.setattr(module, "get", fake_get)
    monkeypatch.setattr(module, "PreviewProcess", child_factory)
    staging = tmp_path / "service"
    staging.mkdir()
    service = module.Service(
        {
            "cache": str(tmp_path / "cache"),
            "staging": str(staging),
            "url": "nats://unused",
            "token": "unused",
            "bucket": "unused",
            "generation": GEN,
            "epoch": EPOCH,
        }
    )
    monkeypatch.setattr(service, "cad_site", lambda cad: tmp_path)
    return service, staging


class _Child:
    """A PreviewProcess stand-in that has already exited with ``result`` written by the time it is polled."""

    def __init__(self, log, module, bootstrap, *, result, stop_error=None, spawn_seconds=0.0):
        time.sleep(spawn_seconds)  # runs in the disk worker, like the real spawn
        self.module, self.log, self.stop_error = module, log, stop_error
        root = Path(bootstrap["root"])
        if result is not None:
            (root / "result.json").write_text(json.dumps(result), encoding="utf-8")
        self.process = SimpleNamespace(poll=lambda: 0)
        log.append(("spawned", module))

    def rss(self):
        return 0

    def stop(self):
        self.log.append(("stop", self.module))
        if self.stop_error:
            raise self.stop_error


@pytest.mark.asyncio
async def test_a_converter_not_proven_over_starts_no_png_child_and_makes_the_service_uncertain(tmp_path, monkeypatch):
    log = []

    def spawn(module, bootstrap, cache, **kwargs):
        if module == CONVERTER:
            return _Child(
                log,
                module,
                bootstrap,
                result={"outcome": "ok", "stage": "mesh_write", "triangles": 12},
                stop_error=RuntimeError("the converter's tree is not proven gone"),
            )
        return _Child(log, module, bootstrap, result={"outcome": "ok"})

    service, staging = _service(tmp_path, monkeypatch, spawn)
    attempt = "a" * 32
    reply = await service.command(_step_command(attempt))
    assert [entry for entry in log if entry[0] == "spawned"] == [("spawned", CONVERTER)]  # no PNG child, ever
    assert service.uncertain and reply == {"outcome": "unavailable"}
    assert (staging / attempt).exists()  # an unproven tree's staging is never cleaned up under it
    # and the uncertain service refuses every later run instead of starting another child beside the live one
    assert await service.command(_step_command("b" * 32, seq=2)) == {"outcome": "unavailable"}
    assert [entry for entry in log if entry[0] == "spawned"] == [("spawned", CONVERTER)]


@pytest.mark.asyncio
async def test_a_cancel_during_the_png_childs_spawn_still_owns_and_stops_that_child(tmp_path, monkeypatch):
    log = []
    spawning = threading.Event()

    def spawn(module, bootstrap, cache, **kwargs):
        if module == CONVERTER:
            return _Child(log, module, bootstrap, result={"outcome": "ok", "stage": "mesh_write", "triangles": 12})
        spawning.set()
        return _Child(log, module, bootstrap, result=None, spawn_seconds=0.5)  # still being created when cancelled

    service, staging = _service(tmp_path, monkeypatch, spawn)
    attempt = "a" * 32
    command = _step_command(attempt)
    run = asyncio.create_task(service.command(command))
    await asyncio.to_thread(spawning.wait, 10)
    cancel = command.wire("cancel")
    reply = await service.command(Command.parse(cancel, GEN, EPOCH))
    await run
    assert ("spawned", "backend.app.preview_render") in log  # the spawn completed under ownership...
    assert log[-1] == ("stop", "backend.app.preview_render")  # ...and that child was stopped before the reply
    assert reply["proof"]["mode"] == "cancel_ack" and not service.uncertain
    assert reply["stage"] == "png"  # a cancelled STEP still names where it was stopped
    assert not (staging / attempt).exists()


# --- the real chain ---------------------------------------------------------------------------------------------


def _sliced() -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 X1 Y2\n")
    return content.getvalue()


def _box() -> bytes:
    import trimesh

    return trimesh.creation.box().export(file_type="stl")


async def _identity(runtime):
    return runtime.service.process.pid, runtime.epoch, len(runtime.restarts), runtime.uncertain


def _descendants(pid: int) -> set[int]:
    try:
        return {p.pid for p in psutil.Process(pid).children(recursive=True)}
    except psutil.NoSuchProcess:
        return set()


def _attempt_children(runtime) -> set[int]:
    """Processes under the service tree beyond the idle service itself (guardians, launchers, consoles)."""
    return _descendants(runtime.service.process.pid) - runtime._idle_tree


def _idle(runtime) -> None:
    runtime._idle_tree = _descendants(runtime.service.process.pid)


def _capture(pids: set[int]) -> dict[int, psutil.Process]:
    """The processes themselves, not their numbers: Windows hands a PID out again quickly, and under a parallel test
    run a reused PID made a dead converter look alive (measured: 2 of 6 under -n 4)."""
    captured = {}
    for pid in pids:
        try:
            captured[pid] = psutil.Process(pid)
        except psutil.NoSuchProcess:
            pass
    return captured


def _alive(procs: dict[int, psutil.Process]) -> set[int]:
    return {pid for pid, proc in procs.items() if proc.is_running()}  # same PID AND same creation time


def _real_service(runtime) -> psutil.Process:
    """The deepest ``preview_service`` process (a venv launcher on Windows sits above the real interpreter)."""

    def is_service(proc):
        try:
            return "backend.app.preview_service" in proc.cmdline()
        except psutil.Error:
            return False

    services = [p for p in psutil.Process(runtime.service.process.pid).children(recursive=True) if is_service(p)]
    return next(p for p in services if not any(is_service(c) for c in p.children()))


async def _converter_meshing(runtime, timeout: float = 60) -> dict[int, psutil.Process]:
    """Barrier: the converter's own stage marker says it is meshing; returns the service's descendants then."""
    service_root = runtime.staging / "service"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for marker in service_root.glob("*/stage"):
            try:
                if marker.read_text(encoding="ascii") == "mesh":
                    return _capture(_attempt_children(runtime))
            except OSError:
                pass
        await asyncio.sleep(0.02)
    raise AssertionError("the converter never reached its mesh stage")


async def _watch_markers(runtime, seen: list) -> None:
    """Every distinct stage marker the converter writes, in order, until cancelled."""
    while True:
        for marker in (runtime.staging / "service").glob("*/stage"):
            try:
                text = marker.read_text(encoding="ascii")
            except OSError:
                continue
            if not seen or seen[-1] != text:
                seen.append(text)
        await asyncio.sleep(0.01)


class _Sampler:
    """Every new descendant of the service seen after ``start`` — a PNG child after a cancel would show here."""

    def __init__(self, runtime, known: set[int]):
        self.runtime, self.known, self.new = runtime, set(known), set()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            self.new |= _attempt_children(self.runtime) - self.known
            time.sleep(0.01)

    def close(self) -> set[int]:
        self.stop.set()
        self.thread.join(5)
        return self.new


def _record_release(runtime) -> list:
    """What the service still had running at the moment main released each slot."""
    seen = []
    real = runtime.slot.release

    def release(ticket=None):
        seen.append((ticket.priority if ticket else None, _attempt_children(runtime)))
        real(ticket)

    runtime.slot.release = release
    return seen


@pytest.fixture(scope="module")
def dense(tmp_path_factory):
    from backend.tests.cad_lane import located
    from backend.tests.unit.services.test_preview_step_runtime import _DENSE

    found = located()
    if found is None or not found.ok:
        return None
    path = tmp_path_factory.mktemp("dense") / "dense.step"
    subprocess.run([sys.executable, "-c", _DENSE, str(found.site), str(path)], cwd=ROOT, check=True, timeout=300)
    return path


@pytest.fixture
async def cad_runtime(tmp_path):
    from backend.tests.cad_lane import cad_app_dir

    rt = PreviewRuntime(tmp_path / "preview", app_dir=cad_app_dir())
    from backend.tests.preview_support import started

    await started(rt)
    try:
        await _warm(rt)
        yield rt
    finally:
        await rt.stop()


async def _warm(runtime) -> None:
    """One picture before any timing: a fresh runtime's first render pays one-time costs in its own MPLCONFIGDIR
    (matplotlib's font cache) — the first sliced picture took 15.8 s against ~1.2 s for the next three on a GitHub
    macOS arm64 runner — which no latency or deadline in these tests is about."""
    async with runtime.attempt({"mesh": (_box(), "stl")}) as result:
        assert result.outcome == "ok"


@pytest.mark.requires_cad
@pytest.mark.asyncio
async def test_a_cancelled_converter_is_reaped_before_the_slot_is_released_and_nothing_follows_it(cad_runtime, dense):
    before = await _identity(cad_runtime)
    _idle(cad_runtime)
    releases = _record_release(cad_runtime)
    task = asyncio.create_task(_attempt_outcome(cad_runtime, dense))
    meshing = await _converter_meshing(cad_runtime)
    assert meshing  # the converter's guardian and child are alive
    sampler = _Sampler(cad_runtime, meshing)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proof_seconds = time.monotonic() - started
    assert not sampler.close()  # no PNG child, no other child after the cancel
    assert releases == [("low", set())]  # the slot went only once the service had no child left
    assert not _alive(meshing)  # every converter process is gone
    assert proof_seconds < C_CANCEL_SECONDS
    assert await _identity(cad_runtime) == before  # a proven end, no retire
    async with cad_runtime.attempt({"mesh": (_box(), "stl")}) as result:
        assert result.outcome == "ok"


async def _attempt_outcome(runtime, path, **kwargs):
    async with runtime.attempt({"mesh": (path, "step")}, **kwargs) as result:
        return result.outcome, result.stage, dict(result.files)


@pytest.mark.requires_cad
@pytest.mark.asyncio
async def test_four_preemptions_in_a_row_reap_the_converter_and_keep_the_service(cad_runtime, dense):
    before = await _identity(cad_runtime)
    _idle(cad_runtime)
    releases = _record_release(cad_runtime)
    latencies = []
    for _ in range(4):
        low = asyncio.create_task(_attempt_outcome(cad_runtime, dense))
        meshing = await _converter_meshing(cad_runtime)
        started = time.monotonic()
        async with cad_runtime.attempt({"sliced": (_sliced(), "3mf"), "mesh": (_box(), "stl")}) as high:
            latencies.append(time.monotonic() - started)
            assert high.outcome == "ok"
            with zipfile.ZipFile(high.files["output"]) as output:
                assert "Metadata/plate_1.png" in output.namelist()  # the sliced output carries its thumbnail
        assert await low == ("preempted", "mesh", {})  # never published after the revoke
        assert meshing and not _alive(meshing)
    assert [r for r in releases if r[0] == "low"] == [("low", set())] * 4
    assert max(latencies) < C_CANCEL_SECONDS + 5
    assert await _identity(cad_runtime) == before


async def _mesh_window(runtime, path) -> tuple[float, float]:
    """One attempt with no short deadline, measured on THIS machine: when its converter begins meshing, and when the
    aggregate-RSS watchdog ends it (``resource_limit`` at ``mesh``, the service kept). Both move with the machine's
    speed — 2.2 s and 4.2 s idle here, the watchdog at 2.7 s on a GitHub macOS arm64 runner — so a fixed deadline
    meant to land while it meshes lands after the watchdog on a faster one."""
    mesh_at = []

    async def watch(started: float):
        while not mesh_at:
            for marker in (runtime.staging / "service").glob("*/stage"):
                try:
                    if marker.read_text(encoding="ascii") == "mesh":
                        mesh_at.append(time.monotonic() - started)
                        break
                except OSError:
                    pass
            await asyncio.sleep(0.005)

    started = time.monotonic()
    watcher = asyncio.create_task(watch(started))
    try:
        result = await _attempt_outcome(runtime, path)
    finally:
        watcher.cancel()
    ended = time.monotonic() - started
    assert result == ("resource_limit", "mesh", {}) and mesh_at, result  # the watchdog ends it mid-mesh
    return mesh_at[0], ended


@pytest.mark.requires_cad
@pytest.mark.asyncio
async def test_four_step_timeouts_keep_the_service_and_an_stl_follows_at_once(cad_runtime, dense):
    """A deadline that lands while the dense STEP meshes ends it as a timeout, four times, with the service kept.

    Where that lies is measured on this machine (``_mesh_window``), but not fixed by it: on a GitHub Windows runner
    one attempt reached its mesh at ~2.5 s and the next had written no stage at all by 3.9 s. So the deadline starts
    halfway inside the measured window and moves after each attempt that missed it — later when it landed before
    the mesh, earlier when the watchdog came first — and only a timeout AT the mesh counts. Every attempt, counted
    or not, publishes nothing, reports a stage never earlier than the last marker seen, and keeps the service."""
    from backend.app.services.preview_protocol import STAGES

    before = await _identity(cad_runtime)
    mesh_at, watchdog_at = await _mesh_window(cad_runtime, dense)
    offset, step = mesh_at + (watchdog_at - mesh_at) / 2, max(0.2, (watchdog_at - mesh_at) / 4)
    landed = []
    while sum(1 for o, s in landed if o == "timeout" and s == "mesh") < 4:
        assert len(landed) < 12, landed  # bounded: a machine whose window cannot be hit says so
        seen = []
        watcher = asyncio.create_task(_watch_markers(cad_runtime, seen))
        try:
            outcome, stage, files = await _attempt_outcome(
                cad_runtime, dense, deadline=time.monotonic_ns() + int(offset * 1e9)
            )
        finally:
            watcher.cancel()
        assert outcome in {"timeout", "canceled", "resource_limit"} and not files, (outcome, stage)
        # A short-lived marker can disappear between watcher polls. Any
        # marker we saw still bounds the stage the service may report.
        assert stage is None or stage in STAGES, (stage, seen)
        if seen:
            assert stage is not None and STAGES.index(stage) >= STAGES.index(seen[-1]), (stage, seen)
        landed.append((outcome, stage))
        if outcome == "resource_limit":
            offset -= step  # the watchdog came first
        elif stage != "mesh":
            offset += step  # it ended before the mesh began
    assert await _identity(cad_runtime) == before
    started = time.monotonic()
    async with cad_runtime.attempt({"mesh": (_box(), "stl")}) as result:
        assert result.outcome == "ok"
    assert time.monotonic() - started < 30


@pytest.mark.requires_cad
@pytest.mark.asyncio
async def test_a_service_that_dies_under_the_converter_takes_the_converter_with_it(cad_runtime, dense):
    old_pid, old_epoch = cad_runtime.service.process.pid, cad_runtime.epoch
    _idle(cad_runtime)
    task = asyncio.create_task(_attempt_outcome(cad_runtime, dense))
    meshing = await _converter_meshing(cad_runtime)
    assert meshing
    _real_service(cad_runtime).kill()  # the converter's parent dies under it
    outcome, _, files = await asyncio.wait_for(task, 60)
    assert outcome == "unavailable" and not files
    end = time.monotonic() + 15
    while _alive(meshing) and time.monotonic() < end:
        await asyncio.sleep(0.1)
    assert not _alive(meshing)  # the converter died with its parent's Job
    end = time.monotonic() + 30
    while not (cad_runtime.ready and cad_runtime.service) and time.monotonic() < end:
        await asyncio.sleep(0.1)
    assert cad_runtime.epoch != old_epoch and cad_runtime.service.process.pid != old_pid
    async with cad_runtime.attempt({"mesh": (_box(), "stl")}) as result:
        assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_a_bundle_that_vanishes_after_mains_gate_is_still_only_cad_unavailable(tmp_path):
    from backend.tests.unit.services.test_preview_step_runtime import fake_broken_bundle

    app = tmp_path / "app"
    fake_broken_bundle(app)
    runtime = PreviewRuntime(tmp_path / "preview", app_dir=app)
    await runtime.start()
    try:
        before = await _identity(runtime)
        real = runtime.cad_descriptor

        async def gate_then_vanish():
            descriptor = await real()
            assert descriptor is not None  # main's gate passed
            shutil.rmtree(app / "runtime")  # ...and the bundle is gone before the service looks
            return descriptor

        runtime.cad_descriptor = gate_then_vanish
        sequence = runtime.sequence
        outcome, stage, files = await _attempt_outcome(runtime, BOX)
        assert (outcome, stage, files) == ("cad_unavailable", "cad_import", {})
        assert runtime.sequence > sequence  # this time the command did reach the service
        async with runtime.attempt({"mesh": (_box(), "stl")}) as result:
            assert result.outcome == "ok"
        assert await _identity(runtime) == before
    finally:
        await runtime.stop()


def test_the_bundle_lives_under_the_app_runtime_folder():
    """The vanish test removes ``<app>/runtime``: that is where bundle_dir puts it."""
    catalog = step_runtime.load_catalog()
    selection = step_runtime.select(catalog)
    target = step_runtime.bundle_dir(Path("app"), selection.key, catalog.digest)
    assert target.parts[:2] == ("app", "runtime")
