"""PartRenderRuntime: one slot, released only after the attempt is proven over (plan E3, task 18)."""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from backend.app.services import part_render_runtime as prr, render_runtime
from backend.app.services.local_worker_broker import LocalWorkerBroker
from backend.app.services.part_render_runtime import PartRenderRuntime
from backend.app.services.part_render_tree import launch, launch_arg, record
from backend.app.services.part_render_types import RenderTask, RuntimeUnavailable, SourceRef
from backend.tests.fixtures.part_render_3mf import two_objects_3mf
from backend.tests.unit.services.test_part_render_node import NODE, needs_pin

ROOT = Path(__file__).resolve().parents[3]
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO is how a read is hung here (POSIX)")


def task_for(source: Path, *, render_id: int = 1, plate: int = 1, sha256: str | None = None) -> RenderTask:
    data = source.read_bytes() if source.is_file() else b""
    return RenderTask(
        render_id=render_id,
        file_sha256=sha256 or hashlib.sha256(data).hexdigest(),
        plate_index=plate,
        renderer_version=2,
        phase="render",
        reason=None,
        attempts=0,
        priority=1,
        has_result=False,
        source=SourceRef(library_file_id=1, path=str(source), root=str(source.parent), kind="3mf", size=len(data)),
    )


def hung(tmp_path: Path) -> RenderTask:
    fifo = tmp_path / "hung.3mf"
    os.mkfifo(fifo)
    return task_for(fifo, render_id=9, sha256="0" * 64)


def deadline(seconds: float) -> int:
    return time.monotonic_ns() + int(seconds * 1e9)


@pytest.fixture
async def live(tmp_path):
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = PartRenderRuntime(tmp_path, broker, app_dir=ROOT)
    await runtime.start()
    try:
        yield runtime, broker
    finally:
        await runtime.stop()
        await broker.stop()


def attempt_tree(runtime: PartRenderRuntime) -> list[psutil.Process]:
    guardian = psutil.Process(runtime.service.process.pid)
    service = [p for p in guardian.children(recursive=True) if "part_render_service" in " ".join(p.cmdline())]
    return [p for s in service for p in s.children(recursive=True)]  # the attempt's guardian and below


async def busy(runtime: PartRenderRuntime, at_least: int = 2) -> list[psutil.Process]:
    for _ in range(800):
        try:
            tree = attempt_tree(runtime)
        except psutil.Error:
            tree = []
        if len(tree) >= at_least:
            return tree
        await asyncio.sleep(0.025)
    raise AssertionError("the attempt never started")


async def recorded(runtime: PartRenderRuntime) -> None:
    """Every process the attempt launched has its record. ``busy`` sees a child the moment it forks, before its
    guardian has written ``child.pid``: a worker killed then leaves a launch caught before its record, which
    main's strict proof refuses by design until the process restarts (``test_part_render_tree``) -- measured on
    macOS 2026-10-10, where that window made this scenario fail one run in many."""
    service = runtime.staging / "service"
    for _ in range(800):
        attempts = [path for path in service.iterdir() if path.is_dir()] if service.is_dir() else []
        launched = [
            (attempt, path.name.removesuffix(".launch")) for attempt in attempts for path in attempt.glob("*.launch")
        ]
        if launched and all((attempt / f"{name}.pid").exists() for attempt, name in launched):
            return
        await asyncio.sleep(0.025)
    raise AssertionError("the attempt's records never completed")


def dead(processes) -> bool:
    def one(process):
        try:
            return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    return all(one(p) for p in processes)


def gone(pid: int) -> bool:
    try:
        return dead([psutil.Process(pid)])
    except psutil.NoSuchProcess:
        return True


def top_guardians() -> list[psutil.Process]:
    """This process's worker guardians, top level only: a venv launcher and its interpreter share one command
    line, so a guardian whose parent is a guardian is the same worker."""

    def guardian(process) -> bool:
        try:
            # the module argument itself: pytest's own command line may name test_worker_guardian.py
            return "backend.app.worker_guardian" in process.cmdline()
        except psutil.Error:
            return False

    return [p for p in psutil.Process().children(recursive=True) if guardian(p) and not guardian(p.parent())]


@pytest.mark.asyncio
async def test_a_fallback_attempt_renders_through_the_real_worker(live, tmp_path, caplog):
    runtime, _ = live
    caplog.set_level(logging.INFO, logger=prr.__name__)
    result = await runtime.render(
        task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="fallback", deadline_ns=deadline(60)
    )
    assert result.outcome == "done" and result.result["outcome"] == "ok"
    assert (result.files / "manifest.json").is_file() and (result.files / "101.lg.png").is_file()
    assert result.runtime_version is None  # no Node in fallback
    await runtime.discard(result)
    assert not result.files.exists()
    started = [r for r in caplog.records if "started" in r.getMessage()]
    ended = [r for r in caplog.records if "outcome=done" in r.getMessage()]
    assert len(started) == 1 and len(ended) == 1 and result.attempt_id[:8] in ended[0].getMessage()


@needs_pin
@pytest.mark.asyncio
async def test_a_full_attempt_renders_with_the_pinned_node(live, tmp_path):
    runtime, _ = live
    result = await runtime.render(
        task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="render", deadline_ns=deadline(120)
    )
    manifest = json.loads((result.files / "manifest.json").read_text(encoding="utf-8"))
    assert {o["identify_id"]: o["method"] for o in manifest["objects"]} == {101: "toolpath", 202: "toolpath"}
    assert result.runtime_version == runtime.node.version
    await runtime.discard(result)


@needs_fifo
@pytest.mark.asyncio
async def test_a_hung_read_holds_the_slot_until_its_tree_is_gone(live, tmp_path, caplog):
    runtime, _ = live
    caplog.set_level(logging.INFO, logger=prr.__name__)
    first = asyncio.create_task(runtime.render(hung(tmp_path), mode="fallback", deadline_ns=deadline(2)))
    tree = await busy(runtime, at_least=2)  # the attempt's guardian and part_render, stuck in open()
    second = asyncio.create_task(
        runtime.render(task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="fallback", deadline_ns=deadline(60))
    )
    first_result = await first
    assert first_result.outcome == "timeout"
    assert dead(tree)
    second_result = await second
    assert second_result.outcome == "done"
    messages = [r.getMessage() for r in caplog.records]
    first_end = next(
        i for i, m in enumerate(messages) if f"attempt={first_result.attempt_id[:8]}" in m and "outcome=" in m
    )
    second_start = next(
        i for i, m in enumerate(messages) if f"attempt={second_result.attempt_id[:8]}" in m and "started" in m
    )
    assert first_end < second_start  # the second attempt started only after the first was over
    await runtime.discard(second_result)


@needs_fifo
@pytest.mark.asyncio
async def test_cancelling_a_render_returns_only_after_the_proof(live, tmp_path):
    runtime, _ = live
    render = asyncio.create_task(runtime.render(hung(tmp_path), mode="fallback", deadline_ns=deadline(120)))
    tree = await busy(runtime)
    render.cancel()
    with pytest.raises(asyncio.CancelledError):
        await render
    assert dead(tree)
    assert not runtime.slot.locked()
    assert runtime.health()["state"] in ("ready", "unavailable")  # unavailable only for the Node reasons
    assert runtime.refusal("fallback") is None


@needs_fifo
@pytest.mark.asyncio
async def test_an_unanswered_run_is_cancelled_and_counted_as_a_timeout(live, tmp_path, monkeypatch):
    runtime, _ = live
    monkeypatch.setattr(prr, "_REPLY_GRACE_SECONDS", 0)  # main gives up at the deadline, before the worker answers
    render = asyncio.create_task(runtime.render(hung(tmp_path), mode="fallback", deadline_ns=deadline(1)))
    tree = await busy(runtime)
    result = await render
    assert result.outcome == "timeout"
    assert dead(tree)
    assert not runtime.uncertain


@needs_fifo
@pytest.mark.asyncio
async def test_a_worker_dying_mid_attempt_leaves_no_orphan_and_restarts(live, tmp_path):
    runtime, broker = live
    render = asyncio.create_task(runtime.render(hung(tmp_path), mode="fallback", deadline_ns=deadline(120)))
    tree = await busy(runtime)
    await recorded(runtime)  # mid-attempt, not inside its spawn window (that case is fail-closed by design)
    old_pid = runtime.service.process.pid
    services = [
        p for p in psutil.Process(old_pid).children(recursive=True) if "part_render_service" in " ".join(p.cmdline())
    ]
    for process in services:
        process.kill()  # the worker dies; its attempt's guardian is orphaned out of the worker's tree
    with pytest.raises(RuntimeUnavailable):
        await render
    assert dead(tree)  # proven through the attempt's records, not through the dead worker's descendants
    # Wait for what is asserted: a new worker that admits the fallback lane. Health is the wrong signal
    # here: without a provisioned Node pin (the Backend Tests job has none) it reports "unavailable"
    # even with a healthy restarted worker, and the loop always ran its full 30 s.
    for _ in range(300):
        if runtime.refusal("fallback") is None and runtime.service and runtime.service.process.pid != old_pid:
            break
        await asyncio.sleep(0.1)
    assert runtime.service is not None and runtime.service.process.pid != old_pid  # it really restarted
    assert runtime.refusal("fallback") is None
    assert broker.nc.is_connected


@pytest.mark.asyncio
async def test_an_unproven_retire_closes_admission_until_restart(tmp_path, monkeypatch):
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = runtime.staging / "service" / ("d" * 32)
    attempt.mkdir(parents=True)
    (attempt / "child.pid").write_text("{broken", encoding="ascii")  # a record nothing can prove gone
    runtime.ready = True
    with pytest.raises(RuntimeUnavailable):
        await runtime.retire()
    assert runtime.uncertain
    assert runtime.health()["reason"] == "ownership_uncertain"
    with pytest.raises(RuntimeUnavailable, match="ownership_uncertain"):
        await runtime.render(task_for(tmp_path / "x.3mf"), mode="fallback", deadline_ns=deadline(5))
    assert attempt.exists()  # nothing is deleted while its owner may live


@pytest.mark.asyncio
async def test_an_unproven_retire_says_why_in_the_parent_log(tmp_path, caplog):
    """The parent log names the record and the check that failed -- "not proven gone" alone could not tell
    the macOS ownership_uncertain of 2026-10-10 from its three other causes."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = runtime.staging / "service" / ("d" * 32)
    attempt.mkdir(parents=True)
    launch(attempt, "child")  # launched, its record never written
    runtime.ready = True
    with (
        caplog.at_level(logging.ERROR, logger="backend.app.services.part_render_runtime"),
        pytest.raises(RuntimeUnavailable),
    ):
        await runtime.retire()
    assert "attempt=dddddddd ownership uncertain: child: launched without its record" in caplog.text


@pytest.mark.asyncio
async def test_a_previous_generations_unproven_attempt_closes_admission_at_start(tmp_path, monkeypatch):
    old = tmp_path / ".cache" / "part-render-service" / "staging" / ("e" * 32) / "service" / ("f" * 32)
    old.mkdir(parents=True)
    (old / "node.pid").write_text("{broken", encoding="ascii")
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    await runtime.start()
    try:
        assert runtime.health()["reason"] == "ownership_uncertain"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_restart_drops_only_its_own_buckets(tmp_path):
    from nats.js.api import ObjectStoreConfig

    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    js = broker.nc.jetstream(timeout=5)
    for name in ("bamdude_partrender_old", "bamdude_library_foreign"):
        await js.create_object_store(bucket=name, config=ObjectStoreConfig(bucket=name, max_bytes=1024**2))
    runtime = PartRenderRuntime(tmp_path, broker, app_dir=ROOT)
    try:
        await runtime.start()
        names = {stream.config.name for stream in await js.streams_info()}
        assert "OBJ_bamdude_partrender_old" not in names
        assert "OBJ_bamdude_library_foreign" in names
        assert f"OBJ_{runtime.bucket}" in names
    finally:
        await runtime.stop()
        await js.delete_object_store("bamdude_library_foreign")
        await broker.stop()


@pytest.mark.asyncio
async def test_the_worker_restarts_without_restarting_the_broker(live):
    runtime, broker = live
    old = runtime.service.process.pid
    runtime.service.process.kill()
    for _ in range(300):
        if runtime.ready and runtime.service and runtime.service.process.pid != old:
            break
        await asyncio.sleep(0.1)
    assert runtime.ready and runtime.service.process.pid != old
    assert broker.nc.is_connected


def test_node_resolution_keeps_its_three_answers_apart(tmp_path, monkeypatch):
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready = True

    def unsupported(app_dir):
        raise render_runtime.UnsupportedPlatform("no official build")

    monkeypatch.setattr(render_runtime, "locate", unsupported)
    runtime.resolve_node()  # what the monitor's refresh does; health() only reads
    assert (runtime.health()["state"], runtime.health()["reason"]) == ("degraded", "no_runtime")
    assert runtime.refusal("render") == "no_runtime" and runtime.refusal("fallback") is None and runtime.unsupported

    monkeypatch.setattr(render_runtime, "locate", lambda app_dir: None)
    runtime.resolve_node()
    assert (runtime.health()["state"], runtime.health()["reason"]) == ("unavailable", "runtime_missing")
    assert runtime.refusal("render") == "runtime_missing" and runtime.refusal("fallback") is None

    install = render_runtime.NodeInstall(executable=tmp_path / "node", version="v24.17.0", platform="linux-x64")
    monkeypatch.setattr(render_runtime, "locate", lambda app_dir: install)

    def mismatch():
        raise prr.NodeRenderError("bundle_mismatch", "changed")

    monkeypatch.setattr(prr, "verify_bundle", mismatch)
    runtime.resolve_node()
    assert runtime.health()["reason"] == "bundle_mismatch"
    assert runtime.refusal("render") == "bundle_mismatch"


@pytest.mark.asyncio
async def test_a_launch_without_its_record_keeps_admission_closed_and_its_proof(tmp_path):
    """Consilium E3-R1: the worker died between spawning the guardian and recording it."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = runtime.staging / "service" / ("d" * 32)
    attempt.mkdir(parents=True)
    (attempt / "guardian.launch").touch()
    runtime.ready = True
    with pytest.raises(RuntimeUnavailable):
        await runtime.retire()
    assert runtime.uncertain and runtime.refusal("fallback") == "ownership_uncertain"
    assert (attempt / "guardian.launch").exists()  # the proof is not deleted


def _fake_node(monkeypatch, tmp_path, answers):
    import subprocess as sp

    install = render_runtime.NodeInstall(executable=tmp_path / "node", version="v24.17.0", platform="linux-x64")
    monkeypatch.setattr(render_runtime, "locate", lambda app_dir: install)
    monkeypatch.setattr(prr, "verify_bundle", lambda: "b" * 64)
    replies = iter(answers)

    def run(cmd, **kwargs):
        answer = next(replies)
        if isinstance(answer, Exception):
            raise answer
        return sp.CompletedProcess(cmd, 0, stdout=answer.encode(), stderr=b"")

    monkeypatch.setattr(prr.subprocess, "run", run)
    return install


@pytest.mark.asyncio
async def test_a_node_that_did_not_start_is_probed_again_and_reopens_the_queue(tmp_path, monkeypatch):
    """Consilium E3-R8: recovery in the same process, no restart and no visit to the System page."""
    _fake_node(monkeypatch, tmp_path, [OSError("Node does not start"), "v24.17.0\n"])
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready = True
    runtime._probe_node()
    assert runtime.refusal("render") == "runtime_failed"
    runtime.node_reprobe_at = 0
    await runtime._reprobe_node_if_due()
    assert runtime.refusal("render") is None


@pytest.mark.asyncio
async def test_a_new_node_installation_is_starting_until_it_is_probed(tmp_path, monkeypatch):
    install = _fake_node(monkeypatch, tmp_path, ["v24.17.0\n", "v24.18.0\n"])
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready = True
    runtime._probe_node()
    assert runtime.refusal("render") is None
    newer = render_runtime.NodeInstall(executable=install.executable, version="v24.18.0", platform="linux-x64")
    monkeypatch.setattr(render_runtime, "locate", lambda app_dir: newer)
    runtime.resolve_node()
    assert runtime.health()["reason"] == "starting"
    await runtime._reprobe_node_if_due()
    assert runtime.refusal("render") is None


def _monitor_node_only(monkeypatch) -> None:
    """The monitor with no worker to keep: Node maintenance alone, and no health read on the way."""
    monkeypatch.setattr(prr, "_NODE_REFRESH_SECONDS", 0)
    monkeypatch.setattr(PartRenderRuntime, "_healthy", lambda self: True)
    monkeypatch.setattr(PartRenderRuntime, "health", lambda self: pytest.fail("readiness waited for a health read"))


async def _until_render_admitted(runtime) -> bool:
    for _ in range(100):
        if runtime.refusal("render") is None:
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.mark.asyncio
async def test_a_node_installed_later_is_found_by_the_monitor_alone(tmp_path, monkeypatch):
    """Consilium E3.2-R3: missing, then installed -- render admitted without a health read or a restart."""
    install = _fake_node(monkeypatch, tmp_path, ["v24.17.0\n"])
    where = {"install": None}
    monkeypatch.setattr(render_runtime, "locate", lambda app_dir: where["install"])
    _monitor_node_only(monkeypatch)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready = True
    await prr.disk(runtime._probe_node)
    assert runtime.refusal("render") == "runtime_missing"
    monitor = asyncio.create_task(runtime._monitor())
    try:
        where["install"] = install
        assert await _until_render_admitted(runtime)
        assert runtime.node == install
    finally:
        runtime.closed = True
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_failed_node_replaced_by_a_working_one_is_taken_by_the_monitor_alone(tmp_path, monkeypatch):
    """Consilium E3.2-R3: a replacement is a new installation -- probed on the next tick, not after the
    failed one's reprobe interval, and without a health read."""
    install = _fake_node(monkeypatch, tmp_path, [OSError("Node does not start"), "v24.18.0\n"])
    where = {"install": install}
    monkeypatch.setattr(render_runtime, "locate", lambda app_dir: where["install"])
    _monitor_node_only(monkeypatch)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready = True
    await prr.disk(runtime._probe_node)
    assert runtime.refusal("render") == "runtime_failed"
    runtime.node_reprobe_at = time.monotonic() + 3600  # the failed installation is not due again for an hour
    monitor = asyncio.create_task(runtime._monitor())
    try:
        where["install"] = render_runtime.NodeInstall(
            executable=install.executable, version="v24.18.0", platform="linux-x64"
        )
        assert await _until_render_admitted(runtime)
    finally:
        runtime.closed = True
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_broker_that_arrives_late_brings_the_runtime_up_by_itself(tmp_path, monkeypatch):
    """Consilium E3-R9: no broker at start, then one -- ready without a restart, one worker only."""
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    given = iter([None])
    monkeypatch.setattr(prr, "get_local_worker_broker", lambda: next(given, broker))
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    try:
        await runtime.start()
        assert runtime.reason == "startup_failed" and runtime.monitor is not None
        for _ in range(300):
            if runtime.ready:
                break
            await asyncio.sleep(0.1)
        assert runtime.ready
        assert len(top_guardians()) == 1  # one worker, never a second beside the first
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_a_live_worker_that_missed_its_ready_is_replaced_by_itself(tmp_path, monkeypatch):
    """Consilium E3.2-R4: alive, connected, its bucket made, never confirmed -- not healthy. The monitor
    retires it with its proof and starts one in its place under the backoff; never two side by side."""
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    monkeypatch.setattr(prr, "_STARTUP_SECONDS", 0)  # the first handshake gets no chance at all
    runtime = PartRenderRuntime(tmp_path, broker, app_dir=ROOT)
    try:
        await runtime.start()
        assert runtime.reason == "startup_failed" and not runtime.ready
        first = runtime.service.process.pid
        assert runtime.service.process.poll() is None and runtime.nc.is_connected and runtime.store is not None
        monkeypatch.setattr(prr, "_STARTUP_SECONDS", 20)  # the dependency is back before the monitor's first tick
        for _ in range(300):
            if runtime.ready:
                break
            await asyncio.sleep(0.1)
        assert runtime.ready and runtime.service.process.pid != first
        assert gone(first)  # retired with its proof, not left beside its replacement
        assert runtime.stats["service_restart_attempts"] >= 1
        assert len(top_guardians()) == 1
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_a_worker_start_whose_cleanup_is_unproven_closes_admission(tmp_path, monkeypatch):
    """Consilium E3.2-R1, main's side: a worker guardian that existed and is not proven gone gets no second
    worker beside it, by the start or by the monitor."""
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    spawned: list[str] = []

    def unproven(module, bootstrap, cache, *, on_spawn=None, launch_token=None):
        spawned.append(module)
        raise prr.SpawnUnproven(4242)

    monkeypatch.setattr(prr, "PreviewProcess", unproven)
    runtime = PartRenderRuntime(tmp_path, broker, app_dir=ROOT)
    try:
        await runtime.start()
        assert runtime.uncertain and runtime.refusal("fallback") == "ownership_uncertain"
        await asyncio.sleep(2.5)  # past the monitor's ticks and its first backoff
        assert spawned == ["backend.app.part_render_service"]
        assert runtime.reason == "ownership_uncertain"
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_a_bucket_that_failed_once_is_created_by_the_recovery(tmp_path, monkeypatch):
    from nats.js.api import ObjectStoreConfig

    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    js = broker.nc.jetstream(timeout=5)
    await js.create_object_store(
        bucket="bamdude_library_foreign", config=ObjectStoreConfig(bucket="bamdude_library_foreign", max_bytes=1024**2)
    )
    real = PartRenderRuntime._create_bucket
    failures = iter([RuntimeError("store unavailable")])

    async def flaky(self, stream_api):
        failure = next(failures, None)
        if failure is not None:
            raise failure
        await real(self, stream_api)

    monkeypatch.setattr(PartRenderRuntime, "_create_bucket", flaky)
    runtime = PartRenderRuntime(tmp_path, broker, app_dir=ROOT)
    try:
        await runtime.start()
        for _ in range(300):
            if runtime.ready:
                break
            await asyncio.sleep(0.1)
        assert runtime.ready
        names = {stream.config.name for stream in await js.streams_info()}
        assert "OBJ_bamdude_library_foreign" in names and f"OBJ_{runtime.bucket}" in names
    finally:
        await runtime.stop()
        await js.delete_object_store("bamdude_library_foreign")
        await broker.stop()


def _main_is_empty(runtime) -> bool:
    main = runtime.staging / "main"
    return not main.exists() or not any(main.iterdir())


@pytest.mark.asyncio
async def test_a_fetch_that_fails_half_way_leaves_no_main_staging(live, tmp_path, monkeypatch):
    """Consilium E3-R10."""
    runtime, _ = live

    async def partial(store, ref, path, deadline):
        await prr.disk(path.write_bytes, b"half a result")
        raise prr.PreviewError("protocol_error")

    monkeypatch.setattr(prr, "get", partial)
    result = await runtime.render(
        task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="fallback", deadline_ns=deadline(60)
    )
    assert (result.outcome, result.files) == ("invalid_output", None)
    assert _main_is_empty(runtime)


@pytest.mark.asyncio
async def test_a_pack_that_does_not_unpack_leaves_no_main_staging(live, tmp_path, monkeypatch):
    runtime, _ = live

    def broken(source, dest, *, limit=None):
        raise prr.PackError("not a packed result")

    monkeypatch.setattr(prr, "unpack", broken)
    result = await runtime.render(
        task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="fallback", deadline_ns=deadline(60)
    )
    assert result.outcome == "invalid_output"
    assert _main_is_empty(runtime)


@pytest.mark.asyncio
async def test_cancelling_during_the_transfer_leaves_no_main_staging(live, tmp_path, monkeypatch):
    runtime, _ = live
    started = asyncio.Event()

    async def stuck(store, ref, path, deadline):
        await prr.disk(path.write_bytes, b"half")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(prr, "get", stuck)
    render = asyncio.create_task(
        runtime.render(task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="fallback", deadline_ns=deadline(60))
    )
    await asyncio.wait_for(started.wait(), 30)
    render.cancel()
    with pytest.raises(asyncio.CancelledError):
        await render
    assert _main_is_empty(runtime)


@pytest.mark.asyncio
async def test_retained_main_staging_stops_admission_past_its_budget(live, tmp_path, monkeypatch):
    runtime, _ = live
    retained = runtime.staging / "main" / ("e" * 32)
    retained.mkdir(parents=True)
    (retained / "result.bin").write_bytes(b"0" * 1024)
    monkeypatch.setattr(prr, "_MAIN_STAGING_BYTES", 512)
    with pytest.raises(RuntimeUnavailable, match="staging_full"):
        await runtime.render(task_for(two_objects_3mf(tmp_path / "f.3mf")), mode="fallback", deadline_ns=deadline(60))
    assert runtime.health()["reason"] == "staging_full"


def _ended(path: Path) -> None:
    import subprocess
    import sys

    from backend.app.services.part_render_tree import record

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    record(path, process.pid)
    process.kill()
    process.wait()


def _earlier_run(tmp_path: Path, *, recorded: bool) -> Path:
    """An earlier run whose main died inside Node's spawn window: node.launch, no node.pid, a fetch directory.
    ``recorded``: everything above Node was recorded and has ended."""
    from backend.app.services.part_render_tree import launch

    generation = tmp_path / ".cache" / "part-render-service" / "staging" / ("e" * 32)
    attempt = generation / "service" / ("f" * 32)
    attempt.mkdir(parents=True)
    (generation / "main" / ("1" * 32)).mkdir(parents=True)
    if recorded:
        for name in ("owner", "worker", "service"):
            _ended(generation / f"{name}.pid")
        for name in ("guardian", "child"):
            launch(attempt, name)
            _ended(attempt / f"{name}.pid")
    launch(attempt, "node")
    return generation


@pytest.mark.asyncio
async def test_an_earlier_run_proven_over_by_its_records_is_admitted_and_its_staging_goes(tmp_path):
    """Final review C2 / consilium E3-I-R1: main died inside a spawn window. Its records prove every parent
    dead, so the unrecorded Node never got its input -- no scan of the process table involved."""
    generation = _earlier_run(tmp_path, recorded=True)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    await runtime.start()
    try:
        assert not runtime.uncertain and runtime.earlier_proven
        assert not generation.exists()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_an_earlier_runs_unrecorded_launch_without_a_proven_parent_keeps_admission_closed(tmp_path):
    generation = _earlier_run(tmp_path, recorded=False)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    await runtime.start()
    try:
        assert runtime.uncertain and runtime.health()["reason"] == "ownership_uncertain"
        assert generation.exists()  # nothing is deleted while its owner may live
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_failed_proof_of_an_earlier_run_closes_admission_and_every_restore(tmp_path, monkeypatch):
    """Consilium E3-I-R2: an error while proving the earlier run keeps its records and refuses admission and
    restore -- two restores at once included."""
    from backend.app.services import part_render_scheduler

    generation = _earlier_run(tmp_path, recorded=True)

    def unreadable(path, **kwargs):
        raise PermissionError("the staging cannot be read")

    monkeypatch.setattr(prr, "generation_gone", unreadable)
    monkeypatch.setattr(prr, "runtime", None)
    monkeypatch.setattr(part_render_scheduler, "scheduler", None)
    try:
        current = await prr.start_part_render_runtime(tmp_path, ROOT)
        assert current.uncertain and not current.earlier_proven
        assert generation.exists()
        answers = await asyncio.gather(
            part_render_scheduler.stop_part_render_scheduler("restore"),
            part_render_scheduler.stop_part_render_scheduler("restore"),
        )
        assert answers == [False, False]
    finally:
        await prr.stop_part_render_runtime()


@pytest.mark.asyncio
async def test_restore_without_a_scheduler_asks_for_a_completed_proof(tmp_path, monkeypatch):
    """Consilium E3-I-R2: a start that failed before the proof ran is no proof either."""
    from backend.app.services import part_render_scheduler

    def broken(self):
        raise OSError("the data directory cannot be written")

    monkeypatch.setattr(PartRenderRuntime, "_directories", broken)
    monkeypatch.setattr(prr, "runtime", None)
    monkeypatch.setattr(part_render_scheduler, "scheduler", None)
    try:
        with pytest.raises(OSError):
            await prr.start_part_render_runtime(tmp_path, ROOT)  # the lifespan logs this and goes on
        assert not await part_render_scheduler.stop_part_render_scheduler("restore")
    finally:
        await prr.stop_part_render_runtime()


@pytest.mark.asyncio
async def test_a_recovered_earlier_run_lets_restore_through(tmp_path, monkeypatch):
    from backend.app.services import part_render_scheduler

    _earlier_run(tmp_path, recorded=True)
    monkeypatch.setattr(prr, "runtime", None)
    monkeypatch.setattr(part_render_scheduler, "scheduler", None)
    try:
        await prr.start_part_render_runtime(tmp_path, ROOT)
        assert await part_render_scheduler.stop_part_render_scheduler("restore")
    finally:
        await prr.stop_part_render_runtime()


@pytest.mark.asyncio
async def test_a_generation_records_its_owner_its_worker_and_the_service(live):
    """Consilium E3-I-R1: what a later start needs to prove this run over -- main, and the worker's parent and
    worker, recorded before either got any input."""
    from backend.app.services.part_render_tree import launch_arg

    runtime, _ = live
    staging = runtime.staging
    assert all((staging / f"{name}.pid").is_file() for name in ("owner", "worker", "service"))
    # and each started with the token of its launch on its command line (consilium r2, R2.1)
    for name in ("worker", "service"):
        token = (staging / f"{name}.launch").read_text(encoding="ascii")
        pid = json.loads((staging / f"{name}.pid").read_text(encoding="ascii"))["pid"]
        assert launch_arg(token) in psutil.Process(pid).cmdline()


def test_health_only_reads(tmp_path, monkeypatch):
    """Final review M4: a System-page poll neither hashes the bundle on the event loop nor races the monitor's
    own resolution; the monitor keeps the installation current (R22)."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    monkeypatch.setattr(PartRenderRuntime, "resolve_node", lambda self: pytest.fail("health() resolved Node"))
    assert runtime.health()["reason"] == "not_started"


@pytest.mark.asyncio
async def test_a_render_without_a_node_is_refused_even_when_the_reason_was_cleared(tmp_path):
    """Final review M4: the installation is read once per attempt -- a render never goes out without Node and
    comes back as a full render made by the fallback alone."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready, runtime.node_reason, runtime.node = True, None, None
    with pytest.raises(RuntimeUnavailable):
        await runtime.render(task_for(tmp_path / "x.3mf"), mode="render", deadline_ns=deadline(5))


@pytest.mark.asyncio
async def test_a_task_too_big_for_the_wire_is_a_failed_attempt_not_a_retired_worker(tmp_path, monkeypatch):
    """Final review M7: a path whose JSON exceeds the wire is the file's problem; the healthy worker stays."""
    import types

    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready, runtime.node_reason = True, None
    runtime.nc = types.SimpleNamespace(is_connected=True)

    async def no_rpc(*args, **kwargs):
        pytest.fail("the oversized command went out")

    async def no_retire():
        pytest.fail("a healthy worker was retired")

    monkeypatch.setattr(runtime, "rpc", no_rpc)
    monkeypatch.setattr(runtime, "retire", no_retire)
    # built by hand: a path this long cannot even be stat()ed on Linux, and main never touches the source
    # (consilium E3-I-R4)
    deep = str(tmp_path / ("ї" * 4000) / "plate.3mf")
    task = RenderTask(
        render_id=1,
        file_sha256="a" * 64,
        plate_index=1,
        renderer_version=2,
        phase="render",
        reason=None,
        attempts=0,
        priority=1,
        has_result=False,
        source=SourceRef(library_file_id=1, path=deep, root=str(tmp_path), kind="3mf", size=1),
    )
    result = await runtime.render(task, mode="fallback", deadline_ns=deadline(5))
    assert result.outcome == "crashed"


def test_main_waits_for_the_workers_proof_and_upload_before_it_gives_up():
    """Final review M5: the worker may spend its whole transfer allowance after the proof; main's grace must
    cover both, or a slow upload of a good result costs a retire."""
    from backend.app import part_render_service

    assert prr._REPLY_GRACE_SECONDS >= part_render_service._TRANSFER_SECONDS + 20


@pytest.mark.asyncio
async def test_the_attempt_end_logs_the_workers_crash_cause(tmp_path, monkeypatch, caplog):
    """Parent-log invariant: a crashed attempt names its sanitized cause, so the next one is diagnosable."""
    import types

    caplog.set_level(logging.INFO, logger=prr.__name__)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready, runtime.node_reason = True, None
    runtime.nc = types.SimpleNamespace(is_connected=True)

    async def crashed(command, timeout):
        return {"outcome": "crashed", "crash": {"stage": "watch", "error": "FileNotFoundError", "path": "/x"}}

    monkeypatch.setattr(runtime, "rpc", crashed)
    result = await runtime.render(task_for(tmp_path / "x.3mf"), mode="fallback", deadline_ns=deadline(5))
    assert (result.outcome, result.crash) == ("crashed", "watch:FileNotFoundError")
    ended = [r.getMessage() for r in caplog.records if "outcome=crashed" in r.getMessage()]
    assert len(ended) == 1 and "crash=watch:FileNotFoundError" in ended[0] and "/x" not in ended[0]


@pytest.mark.asyncio
async def test_a_worker_that_does_not_answer_is_logged_with_the_rpc_stage(tmp_path, monkeypatch, caplog):
    import types

    caplog.set_level(logging.INFO, logger=prr.__name__)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.ready, runtime.node_reason = True, None
    runtime.nc = types.SimpleNamespace(is_connected=True)

    async def silent(command, timeout):
        raise ConnectionResetError("gone")

    async def settled(command):
        return None

    monkeypatch.setattr(runtime, "rpc", silent)
    monkeypatch.setattr(runtime, "_cancel_or_retire", settled)
    result = await runtime.render(task_for(tmp_path / "x.3mf"), mode="fallback", deadline_ns=deadline(5))
    assert (result.outcome, result.crash) == ("crashed", "rpc:ConnectionResetError")
    assert any("crash=rpc:ConnectionResetError" in r.getMessage() for r in caplog.records)


SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def token_holder(token: str) -> subprocess.Popen:
    """A live process carrying a launch's token, as a launched process with no record does."""
    return subprocess.Popen([*SLEEP, launch_arg(token)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def dead_record(path: Path) -> None:
    """The record of a process that has ended."""
    process = subprocess.Popen(SLEEP, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    record(path, process.pid)
    process.kill()
    process.wait()


def this_run(runtime: PartRenderRuntime) -> Path:
    """The runtime's staging as retire finds it after its worker died: its owner, its dead worker, one attempt."""
    runtime.staging.mkdir(parents=True, exist_ok=True)
    record(runtime.staging / "owner.pid", os.getpid())
    for name in ("worker", "service"):
        launch(runtime.staging, name)
        dead_record(runtime.staging / f"{name}.pid")
    attempt = runtime.staging / "service" / ("d" * 32)
    attempt.mkdir(parents=True)
    return attempt


def stop_all(*processes: subprocess.Popen) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.asyncio
async def test_a_retire_in_the_spawn_window_ends_the_unrecorded_child_and_admits_again(tmp_path, caplog):
    """The macOS window of 2026-10-10 (spec part-render-unrecorded-launch §1): the retire kills the recorded
    guardian, ends the child by its token, removes the attempt and leaves the runtime admissible."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = this_run(runtime)
    guardian = token_holder(launch(attempt, "guardian"))
    record(attempt / "guardian.pid", guardian.pid)
    child = token_holder(launch(attempt, "child"))
    runtime.ready = True
    try:
        with caplog.at_level(logging.WARNING, logger="backend.app.services.part_render_runtime"):
            await runtime.retire()
        assert child.wait(timeout=5) is not None and guardian.wait(timeout=5) is not None
        assert not attempt.exists() and not runtime.uncertain
        assert "attempt=dddddddd child launched without its record: ended by its token (holders=" in caplog.text
    finally:
        stop_all(guardian, child)


@pytest.mark.asyncio
async def test_a_shutdown_in_the_spawn_window_ends_the_child(tmp_path):
    """Review focus 4: stop() never raises, and the proof it runs inside its slot ends the child too."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = this_run(runtime)
    launch(attempt, "guardian")
    dead_record(attempt / "guardian.pid")
    child = token_holder(launch(attempt, "child"))
    try:
        await runtime.stop()
        assert child.wait(timeout=5) is not None
        assert not runtime.uncertain and not attempt.exists()
    finally:
        stop_all(child)


@pytest.mark.asyncio
async def test_a_worker_that_outlived_its_stop_is_ended_by_its_record(tmp_path):
    """R2: the worker is positively ended before any attempt is walked -- killed when stop() missed it."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    runtime.staging.mkdir(parents=True)
    record(runtime.staging / "owner.pid", os.getpid())
    worker = subprocess.Popen(SLEEP, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    launch(runtime.staging, "service")
    record(runtime.staging / "service.pid", worker.pid)
    try:
        await runtime.retire()
        assert worker.wait(timeout=5) is not None and not runtime.uncertain
    finally:
        stop_all(worker)


@pytest.mark.asyncio
async def test_an_unproven_worker_record_closes_admission_and_says_why(tmp_path, caplog):
    """T8 (c): R2's new refusal -- a worker not shown dead keeps admission, restore and every proof closed."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = runtime.staging / "service" / ("d" * 32)
    attempt.mkdir(parents=True)
    (runtime.staging / "service.pid").write_text("{broken", encoding="ascii")
    runtime.ready = True
    with (
        caplog.at_level(logging.ERROR, logger="backend.app.services.part_render_runtime"),
        pytest.raises(RuntimeUnavailable),
    ):
        await runtime.retire()
    assert runtime.uncertain and runtime.refusal("fallback") == "ownership_uncertain"
    assert attempt.exists() and (runtime.staging / "service.pid").exists()
    assert "Part render worker records: service: record unreadable" in caplog.text


@pytest.mark.asyncio
async def test_a_runtime_whose_first_worker_never_started_is_not_made_uncertain(tmp_path, monkeypatch):
    """Consilium Q1: a broker unavailable before the first spawn leaves no worker record -- absent records are
    no refusal; only an attempt without its proven parent is (spec §4.3)."""
    monkeypatch.setattr(prr, "get_local_worker_broker", lambda: None)
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    await runtime.start()
    assert not runtime.uncertain
    await runtime.stop()
    assert not runtime.uncertain


@pytest.mark.asyncio
async def test_a_retained_attempt_proven_once_is_proven_again_after_the_worker_is_replaced(tmp_path, monkeypatch):
    """The lemma of spec §4.2: an attempt kept after its proof (its directory could not be removed) is proven
    again at the next retire, when this generation's worker records already name a later worker."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = this_run(runtime)
    launch(attempt, "guardian")  # the first worker died before recording the guardian, which never started
    real_cleanup = prr.cleanup_owned
    retained = SimpleNamespace(status="retained_error", error_type="PermissionError")
    monkeypatch.setattr(prr, "cleanup_owned", lambda path: retained)
    await runtime.retire()
    assert attempt.exists() and not runtime.uncertain
    for name in ("worker", "service"):  # the next worker's records replace the first one's
        launch(runtime.staging, name)
        dead_record(runtime.staging / f"{name}.pid")
    monkeypatch.setattr(prr, "cleanup_owned", real_cleanup)
    await runtime.retire()
    assert not attempt.exists() and not runtime.uncertain


@pytest.mark.asyncio
async def test_a_real_worker_dying_beside_an_unrecorded_child_recovers_and_says_so(live, caplog):
    """The parent-log invariant with a real worker: the window's orphan is ended by main's retire, the parent
    log says so, and a new worker comes up (spec §4.7)."""
    runtime, broker = live
    attempt = runtime.staging / "service" / ("c" * 32)
    attempt.mkdir()
    launch(attempt, "guardian")
    dead_record(attempt / "guardian.pid")
    child = token_holder(launch(attempt, "child"))  # what the 2026-10-10 window left beside the worker
    old_pid = runtime.service.process.pid
    try:
        with caplog.at_level(logging.WARNING, logger="backend.app.services.part_render_runtime"):
            for process in psutil.Process(old_pid).children(recursive=True):
                if "part_render_service" in " ".join(process.cmdline()):
                    process.kill()
            for _ in range(300):
                if runtime.service and runtime.service.process.pid != old_pid and runtime.refusal("fallback") is None:
                    break
                await asyncio.sleep(0.1)
        assert runtime.refusal("fallback") is None and not runtime.uncertain
        assert child.wait(timeout=5) is not None and not attempt.exists()
        assert "attempt=cccccccc child launched without its record: ended by its token" in caplog.text
    finally:
        stop_all(child)


# Consilium UL-R1, independent negative controls (plan review r1, test_unrecorded_review.py): an error inside the
# proof -- a damaged launch file, or any unexpected exception -- is no proof. ownership_uncertain before retire
# returns, restore refused, every record kept; shutdown deletes nothing.


def damaged_window(runtime: PartRenderRuntime) -> tuple[Path, subprocess.Popen]:
    """The spawn window's state whose child.launch is damaged -- bytes that are not ASCII (fault injection, not a
    measured disk failure): a live child carrying the token the file no longer holds."""
    attempt = this_run(runtime)
    launch(attempt, "guardian")
    dead_record(attempt / "guardian.pid")
    child = token_holder(launch(attempt, "child"))
    (attempt / "child.launch").write_bytes(b"\xff")
    return attempt, child


@pytest.mark.asyncio
async def test_a_damaged_launch_token_keeps_restore_closed_and_the_child_untouched(tmp_path):
    from backend.app.services.part_render_scheduler import PartRenderScheduler

    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt, child = damaged_window(runtime)
    try:
        with pytest.raises(RuntimeUnavailable):
            await runtime.retire()
        assert child.poll() is None
        assert not await PartRenderScheduler(runtime, None, tmp_path / "results").stop("restore")
        assert runtime.uncertain and attempt.exists()
    finally:
        stop_all(child)


@pytest.mark.asyncio
async def test_a_shutdown_whose_proof_cannot_read_a_token_keeps_every_record(tmp_path):
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt, child = damaged_window(runtime)
    try:
        await runtime.stop()
        assert child.poll() is None
        assert runtime.uncertain and attempt.exists() and (runtime.staging / "service.pid").exists()
    finally:
        stop_all(child)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "stage"), [("worker_records_unproven", "worker records"), ("tree_unproven", "attempt=dddddddd")]
)
async def test_an_unexpected_error_inside_the_proof_closes_restore_and_keeps_the_records(
    tmp_path, monkeypatch, caplog, phase, stage
):
    """UL-R1: not only a token read -- any exception of R2 or of the attempt walk. The parent log names the stage
    and the error's type, never its text (which may carry a path)."""
    from backend.app.services.part_render_scheduler import PartRenderScheduler

    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = this_run(runtime)
    launch(attempt, "child")

    def fault(*args, **kwargs):
        raise OSError("injected failure reading C:/private/share/plate.3mf")

    monkeypatch.setattr(prr, phase, fault)
    with (
        caplog.at_level(logging.ERROR, logger="backend.app.services.part_render_runtime"),
        pytest.raises(RuntimeUnavailable),
    ):
        await runtime.retire()
    assert not await PartRenderScheduler(runtime, None, tmp_path / "results").stop("restore")
    assert runtime.uncertain and attempt.exists() and (runtime.staging / "service.pid").exists()
    assert f"Part render ownership uncertain: the proof failed at {stage}: OSError" in caplog.text
    assert "private" not in caplog.text


# R3 (spec part-render-unrecorded-launch §4.2; consilium Q3): the order of spawn, retire and shutdown is the
# current callers' -- no lock. The probes block inside the NEW proof phase (R2 and the token walk), not only
# in stop(). Process and RPC boundaries are controlled: no OS children, no broker.


def lifecycle_runtime(tmp_path) -> PartRenderRuntime:
    runtime = PartRenderRuntime(tmp_path, SimpleNamespace(url="unused", token="unused"), app_dir=tmp_path)
    runtime.staging.mkdir(parents=True)
    runtime.store = object()
    runtime.nc = SimpleNamespace(is_connected=True, close=AsyncMock())
    runtime._refresh_node_if_due = AsyncMock()
    runtime._reprobe_node_if_due = AsyncMock()

    async def ready(command, timeout):
        return {"outcome": "ok", "epoch": runtime.epoch, "version": 1, "monotonic_ns": time.monotonic_ns()}

    runtime.rpc = ready
    return runtime


async def until(event: threading.Event) -> None:
    async with asyncio.timeout(8):
        while not event.is_set():
            await asyncio.sleep(0.01)


def gated(trace: list[str], label: str, entered: threading.Event, release: threading.Event, answer=None):
    """A proof phase that waits for the test: it records its start and end, and answers ``answer``."""

    def phase(*args, **kwargs):
        trace.append(f"{label}-start")
        entered.set()
        assert release.wait(8)
        trace.append(f"{label}-end")
        return answer

    return phase


@pytest.mark.asyncio
async def test_the_monitor_starts_no_replacement_until_the_worker_proof_has_finished(tmp_path, monkeypatch):
    """T11 (1)."""
    runtime = lifecycle_runtime(tmp_path)
    entered, release, spawned = threading.Event(), threading.Event(), threading.Event()
    trace: list[str] = []
    monkeypatch.setattr(prr, "worker_records_unproven", gated(trace, "worker-proof", entered, release))
    runtime.service = SimpleNamespace(process=SimpleNamespace(poll=lambda: 1), stop=lambda: None)

    def new_process(*args, **kwargs):
        trace.append("spawn")
        spawned.set()
        return SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=12345), stop=lambda: None)

    monkeypatch.setattr(prr, "PreviewProcess", new_process)
    runtime.monitor = asyncio.create_task(runtime._monitor())
    try:
        await until(entered)
        # longer than the monitor's tick plus its first backoff: a retire that did not wait for the proof would
        # have let the replacement spawn by now
        await asyncio.sleep(2.0)
        assert not spawned.is_set()
        release.set()
        await until(spawned)
        assert trace.index("worker-proof-end") < trace.index("spawn")
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_a_shutdown_during_a_spawn_waits_for_it_then_proves_the_worker(tmp_path, monkeypatch):
    """T11 (2)."""
    runtime = lifecycle_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()
    trace: list[str] = []

    def constructor(*args, **kwargs):
        trace.append("spawn-start")
        entered.set()
        assert release.wait(8)
        trace.append("spawn-end")
        return SimpleNamespace(process=SimpleNamespace(poll=lambda: None, pid=12345), stop=lambda: trace.append("stop"))

    def worker_proof(generation, **kwargs):
        trace.append("worker-proof")

    monkeypatch.setattr(prr, "PreviewProcess", constructor)
    monkeypatch.setattr(prr, "worker_records_unproven", worker_proof)
    runtime.monitor = asyncio.create_task(runtime._monitor())
    stopper = None
    try:
        await until(entered)
        stopper = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0.1)
        assert not stopper.done() and "stop" not in trace
        release.set()
        await asyncio.wait_for(stopper, 8)
        assert trace == ["spawn-start", "spawn-end", "stop", "worker-proof"]
        assert runtime.closed and runtime.service is None
    finally:
        release.set()
        if stopper is not None:
            await stopper
        else:
            await runtime.stop()


@pytest.mark.asyncio
async def test_an_attempt_being_retired_holds_the_slot_and_blocks_a_replacement(tmp_path, monkeypatch):
    """T11 (3): blocked inside the token walk of the attempt's own retire."""
    runtime = lifecycle_runtime(tmp_path)
    (runtime.staging / "service" / ("d" * 32)).mkdir(parents=True)  # the attempt the token walk proves
    entered, release = threading.Event(), threading.Event()
    trace: list[str] = []
    monkeypatch.setattr(prr, "tree_unproven", gated(trace, "token-proof", entered, release))
    runtime._initialize = AsyncMock()
    runtime.service = SimpleNamespace(process=SimpleNamespace(poll=lambda: 1), stop=lambda: None)

    async def active_attempt():
        async with runtime.slot:
            runtime.active_task = asyncio.current_task()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await prr.owned(runtime.retire())
            finally:
                runtime.active_task = None

    active = asyncio.create_task(active_attempt())
    await asyncio.sleep(0)  # the attempt holds the slot before the monitor's first look
    runtime.monitor = asyncio.create_task(runtime._monitor())
    try:
        await until(entered)
        await asyncio.sleep(2.0)  # longer than the monitor's tick plus its first backoff: a replacement would be due
        assert runtime.slot.locked()
        # the attempt's own retire is the only proof running: a monitor that went on would have entered it too
        assert trace.count("token-proof-start") == 1
        runtime._initialize.assert_not_awaited()
        release.set()
        await asyncio.wait_for(active, 8)
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_an_unfinished_proof_keeps_its_records_and_admission_closed(tmp_path, monkeypatch):
    """T11 (4): while the token walk runs nothing is deleted and nothing is admitted; refused, it keeps all."""
    runtime = lifecycle_runtime(tmp_path)
    attempt = runtime.staging / "service" / ("d" * 32)
    attempt.mkdir(parents=True)
    launch(attempt, "child")
    entered, release = threading.Event(), threading.Event()
    trace: list[str] = []
    refusal = "child: launched without its record (parent guardian never launched)"
    monkeypatch.setattr(prr, "tree_unproven", gated(trace, "token-proof", entered, release, answer=refusal))
    runtime.ready = True
    retiring = asyncio.create_task(runtime.retire())
    try:
        await until(entered)
        assert runtime.refusal("fallback") is not None
        assert (attempt / "child.launch").exists()
        release.set()
        with pytest.raises(RuntimeUnavailable):
            await retiring
        assert runtime.uncertain and (attempt / "child.launch").exists()
    finally:
        release.set()


# Final review of the unrecorded-launch change (fix pass): I1 -- an attempt that cannot be listed is no proof; M1.


def spawn_window(runtime: PartRenderRuntime) -> tuple[Path, subprocess.Popen]:
    """The measured window: dead worker records, a dead recorded guardian, a live child with no record."""
    attempt = this_run(runtime)
    launch(attempt, "guardian")
    dead_record(attempt / "guardian.pid")
    return attempt, token_holder(launch(attempt, "child"))


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", ["listing", "entry"])
async def test_staging_that_cannot_be_listed_is_no_proof(tmp_path, monkeypatch, caplog, unreadable):
    """Review I1: abandoned_attempts files a listing error -- or an attempt it cannot stat -- as an unknown entry,
    and nothing waited for an attempt it never saw. An attempt may hide there: no proof, admission and restore
    closed, the child untouched."""
    import errno

    from backend.app.services import worker_staging
    from backend.app.services.part_render_scheduler import PartRenderScheduler

    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt, child = spawn_window(runtime)
    service_root = runtime.staging / "service"
    if unreadable == "listing":
        real = worker_staging._entries

        def entries(path):
            if path == service_root:
                raise PermissionError(errno.EACCES, "denied")
            return real(path)

        monkeypatch.setattr(worker_staging, "_entries", entries)
        stage = "attempts: listing unreadable"
    else:
        real = worker_staging._info

        def info(path):
            if path == attempt:
                raise OSError(errno.EIO, "i/o error")
            return real(path)

        monkeypatch.setattr(worker_staging, "_info", info)
        stage = "attempt=dddddddd: entry unreadable"
    try:
        with (
            caplog.at_level(logging.ERROR, logger="backend.app.services.part_render_runtime"),
            pytest.raises(RuntimeUnavailable),
        ):
            await runtime.retire()
        assert child.poll() is None and attempt.exists()
        assert not await PartRenderScheduler(runtime, None, tmp_path / "results").stop("restore")
        assert runtime.uncertain
        assert f"Part render ownership uncertain: the proof failed at {stage}" in caplog.text
    finally:
        stop_all(child)


@pytest.mark.asyncio
async def test_a_foreign_entry_in_the_staging_stays_a_warning(tmp_path, caplog):
    """Review I1, ruled: an entry whose name is no attempt's (attempt directories are named by a uuid4 hex) cannot
    hide an attempt -- a warning, as before, never a new refusal."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    this_run(runtime)
    (runtime.staging / "service" / "notes.txt").write_text("x", encoding="ascii")
    with caplog.at_level(logging.WARNING, logger="backend.app.services.part_render_runtime"):
        await runtime.retire()
    assert not runtime.uncertain
    assert "Part render worker staging holds an unknown entry: notes.txt" in caplog.text


@pytest.mark.asyncio
async def test_a_launch_ended_before_a_later_error_still_reaches_the_parent_log(tmp_path, monkeypatch, caplog):
    """Review M1: the child was killed by its token, then the walk failed on Node -- the kill is in the log too."""
    from backend.app.services import part_render_tree

    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt, child = spawn_window(runtime)
    launch(attempt, "node")
    real = part_render_tree._unrecorded_why

    def failing(attempt_dir, name, *args):
        if name == "node":
            raise OSError("injected")
        return real(attempt_dir, name, *args)

    monkeypatch.setattr(part_render_tree, "_unrecorded_why", failing)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="backend.app.services.part_render_runtime"),
            pytest.raises(RuntimeUnavailable),
        ):
            await runtime.retire()
        assert child.wait(timeout=5) is not None
        assert "attempt=dddddddd child launched without its record: ended by its token" in caplog.text
        assert "the proof failed at attempt=dddddddd: OSError" in caplog.text
    finally:
        stop_all(child)


# Consilium of the implementation (25c35b77f): UL-I-R1 -- a restore waits for the runtime's own proof, not only the
# scheduler's task; UL-I-M1 -- an end confirmed by the token scan reaches the parent log when a later scan raises.


def monitor_retire_in_proof(tmp_path, monkeypatch, phase: str):
    """The runtime after its worker died while idle: the monitor's retire -- outside the slot -- is held inside the
    named proof phase, which then answers a refusal. Returns the runtime and the phase's two events."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    this_run(runtime)  # its records and one retained attempt directory
    entered, release = threading.Event(), threading.Event()

    def proof(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return "child: injected unproven tree"

    monkeypatch.setattr(prr, phase, proof)
    runtime._refresh_node_if_due = AsyncMock()
    runtime._reprobe_node_if_due = AsyncMock()
    runtime.service = SimpleNamespace(stop=lambda: None, process=SimpleNamespace(poll=lambda: 1))
    runtime.monitor = asyncio.create_task(runtime._monitor())
    return runtime, entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["worker_records_unproven", "tree_unproven"])
async def test_a_restore_waits_for_the_monitors_own_proof(tmp_path, monkeypatch, phase):
    """UL-I-R1 (independent test of the consilium, both phases): the monitor's retire runs outside the slot, so a
    restore that only stopped the scheduler's task answered True while that proof was still running -- and the
    proof then refused. The restore now joins the runtime's stop and answers its finished proof."""
    from backend.app.services.part_render_scheduler import PartRenderScheduler

    runtime, entered, release = monitor_retire_in_proof(tmp_path, monkeypatch, phase)
    scheduler = PartRenderScheduler(runtime, None, tmp_path / "results")
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stop = asyncio.create_task(scheduler.stop("restore"))
        await asyncio.sleep(0.1)
        assert not stop.done()  # no answer while the proof runs
        release.set()
        assert not await asyncio.wait_for(stop, 10)
        assert runtime.uncertain and runtime.closed
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(runtime.monitor, return_exceptions=True), 10)


@pytest.mark.asyncio
async def test_a_restore_before_the_scheduler_started_waits_for_the_runtimes_proof(tmp_path, monkeypatch):
    """UL-I-R1, the branch without a scheduler: the runtime's proof is joined there too."""
    from backend.app.services import part_render_scheduler

    runtime, entered, release = monitor_retire_in_proof(tmp_path, monkeypatch, "worker_records_unproven")
    runtime.earlier_proven = True
    monkeypatch.setattr(part_render_scheduler, "scheduler", None)
    monkeypatch.setattr(prr, "runtime", runtime)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        stop = asyncio.create_task(part_render_scheduler.stop_part_render_scheduler("restore"))
        await asyncio.sleep(0.1)
        assert not stop.done()
        release.set()
        assert not await asyncio.wait_for(stop, 10)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(runtime.monitor, return_exceptions=True), 10)


@pytest.mark.asyncio
async def test_every_stop_of_a_runtime_shares_one_stop_and_its_proof(tmp_path, monkeypatch):
    """UL-I-R1: a restore stops the runtime and the lifespan stops it again; two at once, or one after the other,
    run ONE stop -- one proof, one answer -- never a second retire over records the first already removed."""
    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    this_run(runtime)
    proofs: list[Path] = []
    real = prr.worker_records_unproven

    def counted(generation, **kwargs):
        proofs.append(generation)
        return real(generation, **kwargs)

    monkeypatch.setattr(prr, "worker_records_unproven", counted)
    await asyncio.gather(runtime.stop(), runtime.stop())
    await runtime.stop()
    assert len(proofs) == 1 and not runtime.uncertain and runtime.closed


@pytest.mark.asyncio
async def test_an_end_confirmed_before_a_scan_raises_reaches_the_parent_log(tmp_path, monkeypatch, caplog):
    """UL-I-M1 (independent test of the consilium): the child is killed and seen to end by its token, then the
    next round of the scan raises. The confirmed end is in the parent log beside the proof's failure -- never the
    exception's text."""
    from backend.app.services import part_render_tree

    runtime = PartRenderRuntime(tmp_path, None, app_dir=ROOT)
    attempt = this_run(runtime)
    launch(attempt, "guardian")
    dead_record(attempt / "guardian.pid")
    token = launch(attempt, "child")
    child = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(60)", launch_arg(token)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    real = part_render_tree._holders_why

    def scan(marker, owner):
        if marker == launch_arg(token) and child.poll() is not None:
            raise OSError("C:/private/path")
        return real(marker, owner)

    monkeypatch.setattr(part_render_tree, "_holders_why", scan)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="backend.app.services.part_render_runtime"),
            pytest.raises(RuntimeUnavailable),
        ):
            await runtime.retire()
        assert child.poll() is not None and runtime.uncertain and attempt.exists()
        assert "attempt=dddddddd child launched without its record: 1 token holders ended before the proof failed" in (
            caplog.text
        )
        assert "the proof failed at attempt=dddddddd: OSError" in caplog.text and "private" not in caplog.text
    finally:
        stop_all(child)
