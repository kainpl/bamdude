"""Real broker + independent service + renderer; no printer or application DB."""

import asyncio
import io
import logging
import os
import time
import uuid
import zipfile
from dataclasses import replace

import pytest

from backend.app.services.preview_artifacts import describe, disk, get, put
from backend.app.services.preview_protocol import Command, PreviewError
from backend.app.services.preview_runtime import PreviewRuntime


@pytest.mark.asyncio
async def test_preview_cancel_forwards_service_cleanup_diagnostic(tmp_path):
    from backend.app.preview_service import Service

    service = Service({"generation": "a" * 32, "epoch": "b" * 32})
    command = Command(1, "a" * 32, "b" * 32, "c" * 32, 1, "run", time.monotonic_ns() + 10**9, ())
    service.active = command

    async def running():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"outcome": "canceled", "staging_cleanup": {"path": str(tmp_path), "error": "PermissionError"}}

    service.task = asyncio.create_task(running())
    await asyncio.sleep(0)
    reply = await service.command(replace(command, operation="cancel"))
    assert reply["outcome"] == "canceled"
    assert reply["staging_cleanup"]["error"] == "PermissionError"


@pytest.mark.asyncio
async def test_main_cleanup_failure_preserves_preview_result_and_explains_next_admission(tmp_path, monkeypatch, caplog):
    import trimesh

    from backend.app.services import preview_runtime
    from backend.app.services.worker_staging import CleanupResult

    runtime = PreviewRuntime(tmp_path / "preview")
    await runtime.start()
    original = preview_runtime.cleanup_owned
    denied = False

    def cleanup(path, **kwargs):
        nonlocal denied
        if not denied and path.parent == runtime.staging / "main":
            denied = True
            return CleanupResult("retained_error", path, "PermissionError", 5)
        return original(path, **kwargs)

    monkeypatch.setattr(preview_runtime, "cleanup_owned", cleanup)
    mesh = trimesh.creation.box().export(file_type="stl")
    try:
        with caplog.at_level(logging.WARNING, logger="backend.app.services.preview_runtime"):
            async with runtime.attempt({"mesh": (mesh, "stl")}) as result:
                assert result.outcome == "ok" and result.files["preview"].exists()
            assert denied
            assert "staging_cleanup_failed: phase=main_attempt" in caplog.text
            async with runtime.attempt({"mesh": (mesh, "stl")}) as result:
                assert result.outcome == "resource_limit"
            assert "staging_residue_blocks_admission" in caplog.text
    finally:
        monkeypatch.setattr(preview_runtime, "cleanup_owned", original)
        await runtime.stop()


@pytest.fixture
async def local_runtime(tmp_path):
    service = PreviewRuntime(tmp_path / "preview")
    await service.start()
    assert service.ready
    try:
        yield service
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_readiness_failure_is_fail_soft_and_does_not_leak(tmp_path, monkeypatch):
    service = PreviewRuntime(tmp_path / "failed readiness")

    async def failed():
        raise PreviewError("unavailable")

    monkeypatch.setattr(service, "_launch", failed)
    await service.start()
    assert not service.ready
    async with service.attempt({"mesh": (b"x", "stl")}) as result:
        assert result.outcome == "unavailable"
    assert not service.slot.locked()
    replacement = PreviewRuntime(service.root)
    await replacement.start()
    try:
        assert replacement.ready
    finally:
        await replacement.stop()


@pytest.mark.asyncio
async def test_empty_retired_preview_skeleton_is_not_warned_on_two_starts(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="backend.app.services.preview_runtime")
    root = tmp_path / "preview"
    old = root / "staging" / ("a" * 32)
    (old / "main").mkdir(parents=True)
    (old / "service").mkdir()
    for _ in range(2):
        runtime = PreviewRuntime(root)
        await runtime.start()
        assert runtime.ready
        await runtime.stop()
    assert old.exists()
    assert caplog.text.count("empty_skeleton_count=1") == 2
    assert "Retained preview staging:" not in caplog.text


@pytest.mark.asyncio
async def test_private_authenticated_broker_policy(local_runtime):
    from urllib.parse import urlparse

    import nats
    from nats.errors import Error as NatsError

    assert urlparse(local_runtime.broker.url).hostname == "127.0.0.1"
    status = await local_runtime.store.status()
    assert status.ttl == 900
    assert status.stream_info.config.max_bytes == 2 * 1024**3
    assert local_runtime.nc.max_payload == 1024**2
    # nats-py reports handshake auth failures as its base Error, not the
    # post-connect AuthorizationError subtype.
    with pytest.raises(NatsError, match="Authorization Violation"):
        await nats.connect(local_runtime.broker.url, token="wrong", allow_reconnect=False, connect_timeout=0.2)


@pytest.mark.asyncio
async def test_stream_larger_than_broker_payload_and_immutable(local_runtime, tmp_path):
    source = tmp_path / "large.stl"
    source.write_bytes(os.urandom(3 * 1024**2))
    deadline = time.monotonic_ns() + 30_000_000_000
    ref = await disk(describe, source, uuid.uuid4().hex, "mesh", "stl", deadline)
    await put(local_runtime.store, source, ref, deadline)
    target = tmp_path / "download.stl"
    await get(local_runtime.store, ref, target, deadline)
    assert target.read_bytes() == source.read_bytes()
    with pytest.raises(PreviewError):
        await put(local_runtime.store, source, ref, deadline)


@pytest.mark.asyncio
async def test_cancel_before_run_and_stale_epoch(local_runtime):
    command = local_runtime._command("cancel")
    assert (await local_runtime._rpc(command))["outcome"] == "canceled"
    assert (await local_runtime._rpc(command, "run"))["outcome"] == "canceled"
    wrong = Command(**{**command.__dict__, "service_epoch": "0" * 32})
    # Wrong epochs have a different subject and cannot reach this worker.
    from nats.errors import NoRespondersError

    with pytest.raises(NoRespondersError):
        await local_runtime._rpc(wrong)

    for _ in range(260):
        await local_runtime._rpc(local_runtime._command("cancel"))
    assert (await local_runtime._rpc(command, "run"))["outcome"] == "canceled"


@pytest.mark.asyncio
async def test_bad_digest_refuses_publication(local_runtime, tmp_path):
    from dataclasses import replace

    source = tmp_path / "input.stl"
    source.write_bytes(b"not a model")
    deadline = time.monotonic_ns() + 5_000_000_000
    ref = await disk(describe, source, uuid.uuid4().hex, "mesh", "stl", deadline)
    await put(local_runtime.store, source, ref, deadline)
    with pytest.raises(PreviewError, match="protocol_error"):
        await get(local_runtime.store, replace(ref, digest="0" * 64), tmp_path / "output.stl", deadline)
    assert not (tmp_path / "output.stl").exists()


@pytest.mark.asyncio
async def test_store_lock_is_fail_soft_and_clean_restart(tmp_path):
    first = PreviewRuntime(tmp_path / "shared")
    second = PreviewRuntime(tmp_path / "shared")
    await first.start()
    assert first.ready
    try:
        await second.start()
        assert not second.ready
        assert first.ready
    finally:
        await second.stop()
        await first.stop()
    third = PreviewRuntime(tmp_path / "shared")
    await third.start()
    try:
        assert third.ready
    finally:
        await third.stop()


@pytest.mark.asyncio
async def test_externally_stopped_broker_recovers_on_restart(tmp_path, caplog):
    import json

    import psutil

    first = PreviewRuntime(tmp_path / "crashed")
    await first.start()
    assert first.ready
    marker = first.root / "broker/managed-runtime.json"
    metadata = json.loads(marker.read_text())
    child = psutil.Process(metadata["child_pid"])
    assert child.ppid() == os.getpid()  # Only terminate the broker this test owns.
    try:
        child.terminate()
        await disk(child.wait, 5)
    finally:
        await first.stop()
    assert marker.exists()
    assert "RecoveryRequired" in caplog.text
    replacement = PreviewRuntime(first.root)
    try:
        await replacement.start()
        assert replacement.ready
        assert replacement.broker.recovered_generation == metadata["generation"]
        assert replacement.health().reason is None
        assert not replacement.health().recovery_required
        assert replacement.health().error_type is None
        assert json.loads(marker.read_text())["generation"] != metadata["generation"]
        assert "recovered abandoned generation=" in caplog.text
    finally:
        await replacement.stop()
    assert not marker.exists()


@pytest.mark.asyncio
async def test_legacy_marker_stays_manual_and_diagnostic(tmp_path, caplog):
    import json

    from embedded_nats import NatsServer, RecoveryRequired

    root = tmp_path / "legacy"
    broker = NatsServer(root / "broker").start()
    broker._process.kill()
    broker._process.wait(timeout=5)
    with pytest.raises(RecoveryRequired):
        broker.stop()
    marker = broker.recovery_marker_path
    metadata = json.loads(marker.read_text())
    del metadata["schema"]
    marker.write_text(json.dumps(metadata))
    before = marker.read_bytes()
    service = PreviewRuntime(root)
    await service.start()
    try:
        assert not service.ready
        assert service.health().recovery_required
        assert service.health().reason == "recovery_required"
        assert marker.read_bytes() == before
        assert str(marker) in caplog.text
        assert "unknown_marker" in caplog.text
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_old_staging_is_retained_even_across_clean_restarts(tmp_path, caplog):
    root = tmp_path / "retained"
    old = root / "staging" / ("a" * 32)
    old.mkdir(parents=True)
    artifact = old / "still-in-use.stl"
    artifact.write_bytes(b"old worker may still be reading")
    for _ in range(2):
        service = PreviewRuntime(root)
        await service.start()
        try:
            assert service.ready
            assert artifact.read_bytes() == b"old worker may still be reading"
        finally:
            await service.stop()
        assert not service.staging.exists()  # Own generation is reaped and cleaned.
    assert str(old) in caplog.text
    assert "ownership is unproven" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only versus group SIGTERM")
@pytest.mark.parametrize("whole_group", [False, True])
def test_broker_signal_order_controls_clean_restart(tmp_path, whole_group):
    import signal
    import subprocess
    import sys

    script = """
import signal, sys, time
from pathlib import Path
from embedded_nats import NatsServer, RecoveryRequired
root = Path(sys.argv[1])
stopping = False
def stop_requested(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop_requested)
broker = NatsServer(root / 'broker', auth_token='test-only-signal-order')
broker.start()
(root / 'ready').touch()
while not stopping:
    time.sleep(0.01)
# Model lifespan work before broker.stop; group SIGTERM has already hit NATS.
time.sleep(0.5)
try:
    broker.stop()
except RecoveryRequired:
    print('RECOVERY_REQUIRED', flush=True)
else:
    print('CLEAN_STOP', flush=True)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready").exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        if whole_group:
            os.killpg(owner.pid, signal.SIGTERM)
        else:
            owner.send_signal(signal.SIGTERM)
        output, error = owner.communicate(timeout=15)
        assert owner.returncode == 0, error
        assert ("RECOVERY_REQUIRED" if whole_group else "CLEAN_STOP") in output
        assert (tmp_path / "broker/managed-runtime.json").exists() is whole_group
    finally:
        try:
            os.killpg(owner.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        owner.communicate(timeout=5)


@pytest.mark.asyncio
async def test_real_obj_and_source_checkpoint_preserve_gcode(local_runtime, tmp_path):
    import trimesh

    mesh = trimesh.creation.box()
    obj = tmp_path / "cube.obj"
    obj.write_text(mesh.export(file_type="obj"))
    async with local_runtime.attempt({"mesh": (obj, "obj")}) as result:
        assert result.outcome == "ok"
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 X1 Y2\n")
        archive.writestr("Metadata/plate_2.gcode", "G1 X3 Y4\n")
    async with local_runtime.attempt(
        {"sliced": (content.getvalue(), "3mf"), "mesh": (mesh.export(file_type="stl"), "stl")}
    ) as result:
        assert result.outcome == "ok"
        assert {"preview", "checkpoint", "output"} == set(result.files)
        with zipfile.ZipFile(result.files["output"]) as archive:
            assert archive.read("Metadata/plate_1.gcode") == b"G1 X1 Y2\n"
            assert archive.read("Metadata/plate_2.gcode") == b"G1 X3 Y4\n"
            assert archive.read("Metadata/plate_1.png").startswith(b"\x89PNG")
            assert len(archive.namelist()) == len(set(archive.namelist()))
            existing_png = archive.read("Metadata/plate_1.png")
    # An embedded cover wins even over an invalid source model; source stage
    # must not try to render it or replace the slicer's own bytes.
    embedded = io.BytesIO()
    with zipfile.ZipFile(embedded, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 X7\n")
        archive.writestr("Metadata/plate_1.png", existing_png)
    async with local_runtime.attempt({"sliced": (embedded.getvalue(), "3mf"), "mesh": (b"bad", "stl")}) as result:
        assert result.outcome == "ok"
        assert "preview" not in result.files
        with zipfile.ZipFile(result.files["output"]) as archive:
            assert archive.read("Metadata/plate_1.png") == existing_png


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancellation_reaps_and_releases_slot(local_runtime, monkeypatch, repeat_cancel):
    import backend.app.services.preview_runtime as module

    entered = asyncio.Event()
    original = module.put

    async def held(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "put", held)
    reaping = asyncio.Event()
    release = asyncio.Event()
    retire = local_runtime._retire

    async def held_retire():
        reaping.set()
        await release.wait()
        await retire()

    if repeat_cancel:
        monkeypatch.setattr(local_runtime, "_retire", held_retire)

    async def caller():
        async with local_runtime.attempt({"mesh": (b"test", "stl")}):
            pytest.fail("canceled attempt must not publish")

    task = asyncio.create_task(caller())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    if repeat_cancel:
        await asyncio.wait_for(reaping.wait(), 5)
        task.cancel()
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert not local_runtime.slot.locked()
    assert local_runtime.service is None
    monkeypatch.setattr(module, "put", original)


@pytest.mark.asyncio
async def test_broker_loss_preserves_received_checkpoint(local_runtime, tmp_path):
    import trimesh

    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 X1\n")

    async def stop_after_checkpoint():
        for _ in range(1500):
            if list((local_runtime.staging / "main").glob("*/checkpoint.3mf")):
                await disk(local_runtime.broker.stop)
                return
            await asyncio.sleep(0.01)
        pytest.fail("main did not receive checkpoint")

    stopper = asyncio.create_task(stop_after_checkpoint())
    async with local_runtime.attempt(
        {"sliced": (content.getvalue(), "3mf"), "mesh": (trimesh.creation.box().export(file_type="stl"), "stl")}
    ) as result:
        assert "checkpoint" in result.files
        with zipfile.ZipFile(result.files["checkpoint"]) as archive:
            assert archive.read("Metadata/plate_1.png").startswith(b"\x89PNG")
    await stopper
    assert not local_runtime.ready


@pytest.mark.asyncio
async def test_overload_is_bounded_and_waiters_expire(local_runtime, monkeypatch):
    import backend.app.services.preview_runtime as module

    monkeypatch.setattr(module, "QUEUE_SECONDS", 0.05)
    await local_runtime.slot.acquire()
    try:

        async def waiting():
            async with local_runtime.attempt({"mesh": (b"x", "stl")}) as result:
                return result.outcome

        assert await asyncio.gather(*(waiting() for _ in range(12))) == ["busy"] * 12
        assert local_runtime.waiters == 0
        assert not list((local_runtime.staging / "main").iterdir())
    finally:
        local_runtime.slot.release()


@pytest.mark.asyncio
async def test_quota_admission_and_bad_mesh_recover(local_runtime, monkeypatch):
    import trimesh

    import backend.app.services.preview_runtime as module

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "BUCKET_BYTES", module.ATTEMPT_BYTES)
        async with local_runtime.attempt({"mesh": (b"bad", "stl")}) as result:
            assert result.outcome == "resource_limit"
    async with local_runtime.attempt({"mesh": (b"bad", "stl")}) as result:
        assert result.outcome == "render_failed"
    async with local_runtime.attempt({"mesh": (trimesh.creation.box().export(file_type="stl"), "stl")}) as result:
        assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_deadline_reaps_and_service_crash_restarts_new_epoch(local_runtime, monkeypatch):
    import trimesh

    import backend.app.services.preview_runtime as module

    mesh = trimesh.creation.icosphere(subdivisions=5).export(file_type="stl")
    with monkeypatch.context() as scoped:
        scoped.setattr(module, "ACTIVE_SECONDS", 0.2)
        async with local_runtime.attempt({"mesh": (mesh, "stl")}) as result:
            assert result.outcome == "timeout"
    assert not local_runtime.slot.locked()
    old_epoch = local_runtime.epoch
    if local_runtime.service:
        local_runtime.service.process.kill()
    async with asyncio.timeout(15):
        while local_runtime.epoch == old_epoch or not local_runtime.ready:
            await asyncio.sleep(0.05)
    async with local_runtime.attempt({"mesh": (trimesh.creation.box().export(file_type="stl"), "stl")}) as result:
        assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_crashed_service_attempt_residue_is_cleaned_before_relaunch(local_runtime):
    """Regression for the service-residue probe: same generation must recover."""
    import psutil
    import trimesh

    mesh = trimesh.creation.icosphere(subdivisions=5).export(file_type="stl")
    service_root = local_runtime.staging / "service"

    async def first_attempt():
        async with local_runtime.attempt({"mesh": (mesh, "stl")}) as result:
            return result.outcome

    task = asyncio.create_task(first_attempt())
    async with asyncio.timeout(30):
        while not list(service_root.glob("*/mesh.stl")):
            await asyncio.sleep(0.02)
    guardian = psutil.Process(local_runtime.service.process.pid)
    candidates = [
        child for child in guardian.children(recursive=True) if "backend.app.preview_service" in child.cmdline()
    ]
    assert candidates
    # A venv launcher and interpreter can both match; they must form one
    # ancestry chain, and the actual interpreter is the deepest member.
    for left in candidates:
        for right in candidates:
            if left != right:
                assert left in right.parents() or right in left.parents()
    service = max(candidates, key=lambda child: len(child.parents()))
    service.kill()
    assert await asyncio.wait_for(task, 60) == "unavailable"
    async with asyncio.timeout(30):
        while not local_runtime.ready:
            await asyncio.sleep(0.1)
    assert not list(service_root.glob("*/mesh.stl"))
    async with local_runtime.attempt({"mesh": (trimesh.creation.box().export(file_type="stl"), "stl")}) as result:
        assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_real_object_ttl_and_quota(local_runtime, tmp_path):
    from nats.js.api import ObjectStoreConfig
    from nats.js.errors import APIError, ObjectNotFoundError

    js = local_runtime.nc.jetstream(timeout=2)
    name = "bamdude_preview_test_" + uuid.uuid4().hex
    store = await js.create_object_store(
        bucket=name, config=ObjectStoreConfig(bucket=name, ttl=0.2, max_bytes=256 * 1024)
    )
    try:
        source = tmp_path / "small.stl"
        source.write_bytes(b"x" * 1024)
        deadline = time.monotonic_ns() + 10_000_000_000
        ref = await disk(describe, source, uuid.uuid4().hex, "mesh", "stl", deadline)
        await put(store, source, ref, deadline)
        async with asyncio.timeout(3):
            while True:
                try:
                    await store.get_info(ref.key)
                except ObjectNotFoundError:
                    break
                await asyncio.sleep(0.05)
        source.write_bytes(b"x" * 1024**2)
        ref = await disk(describe, source, uuid.uuid4().hex, "mesh", "stl", deadline)
        with pytest.raises(APIError):
            await put(store, source, ref, deadline)
        assert (await store.status()).size <= 256 * 1024
    finally:
        await js.delete_object_store(name)


def test_owner_crash_kills_guardian_and_renderer(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    import psutil
    import trimesh

    root = Path(__file__).resolve().parents[4]
    (tmp_path / "mesh.stl").write_bytes(trimesh.creation.icosphere(subdivisions=6).export(file_type="stl"))
    script = """
import json, sys, time
from pathlib import Path
from backend.app.services.preview_process import PreviewProcess
p = Path(sys.argv[1])
child = PreviewProcess("backend.app.preview_render", {"root": str(p), "operation": "mesh", "kind": "stl", "deadline": time.monotonic_ns()+120_000_000_000}, p / "cache")
print(child.process.pid, flush=True)
time.sleep(60)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=root,
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    children = []
    try:
        guardian_pid = int(owner.stdout.readline())
        for _ in range(100):
            children = psutil.Process(guardian_pid).children(recursive=True)
            if children:
                break
            time.sleep(0.02)
        assert children
        processes = [psutil.Process(guardian_pid), *children]
        owner.kill()
        owner.wait(timeout=5)
        _, alive = psutil.wait_procs(processes, timeout=10)
        # Docker PID1 can retain dead zombies; zombies are not renderers.
        assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE]
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        owner.stdout.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows embedded Python layout acceptance")
def test_cached_windows_embedded_python_layout(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    staged = root / "installers/windows/build/staging/python"
    if not (staged / "python.exe").exists():
        pytest.skip("no cached installer Python; installer verifier covers wheel presence in CI builds")
    # Do not mutate the previous installer staging, rebuild a bundle or install
    # anything. Copy only the small embedded stdlib/runtime; use tested deps.
    isolated = tmp_path / "embedded Python Україна"
    isolated.mkdir()
    for path in staged.iterdir():
        if path.is_file() and path.suffix in {".exe", ".dll", ".pyd", ".zip"}:
            shutil.copy2(path, isolated / path.name)
    (isolated / "python312._pth").write_text(
        f"python312.zip\n.\n{root}\n{root / 'venv/Lib/site-packages'}\nimport site\n", encoding="utf-8"
    )
    script = """
import asyncio, sys
from pathlib import Path
from backend.app.services.preview_runtime import PreviewRuntime
async def main():
    import trimesh
    runtime = PreviewRuntime(Path(sys.argv[1]))
    try:
        await runtime.start()
        assert runtime.ready
        async with runtime.attempt({'mesh': (trimesh.creation.box().export(file_type='stl'), 'stl')}) as result:
            assert result.outcome == 'ok', result.outcome
            assert result.files['preview'].read_bytes().startswith(b'\\x89PNG')
    finally:
        await runtime.stop()
    assert not runtime.slot.locked()
asyncio.run(main())
print('EMBEDDED_PREVIEW_OK')
"""
    result = subprocess.run(
        [str(isolated / "python.exe"), "-c", script, str(tmp_path / "data cache")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env={**os.environ, "BAMDUDE_IGNORE_DOTENV": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EMBEDDED_PREVIEW_OK" in result.stdout


@pytest.mark.asyncio
async def test_mixed_parser_preview_keeps_http_and_loop_responsive(local_runtime, tmp_path):
    import json
    import statistics
    from types import SimpleNamespace

    import httpx
    import psutil
    import trimesh
    from fastapi import FastAPI

    from backend.app.core.websocket import ConnectionManager
    from backend.app.services import analysis_runtime
    from backend.app.services.analysis_runtime import AnalysisRuntime
    from backend.app.services.print_file_analysis import get_print_file_analysis
    from backend.app.services.printer_manager import PrinterManager

    delivered = asyncio.Event()

    class Socket:
        async def accept(self):
            pass

        async def send_text(self, value):
            assert json.loads(value)["type"] == "probe"
            delivered.set()

        async def close(self, **kwargs):
            pass

    sockets = ConnectionManager()
    await sockets.connect(Socket())

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    manager = PrinterManager()
    (tmp_path / "archive").mkdir()
    archive_path = tmp_path / "archive" / "print.3mf"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Metadata/slice_info.config", '<config><filament id="1" used_g="12.5" type="PLA" /></config>')
        archive.writestr("Metadata/plate_1.gcode", b"M73 L1\nM620 S0\nG1 E2\n" * 10000)
    mesh = trimesh.creation.icosphere(subdivisions=5).export(file_type="stl")
    parser = AnalysisRuntime(tmp_path, SimpleNamespace(url=local_runtime.broker.url, token=local_runtime.token))
    await parser.start()
    assert parser.ready
    analysis_runtime.runtime = parser
    latencies, gaps, memory, disks, ws_latencies, baseline = [], [], [], [], [], []
    cpu = {}
    started = time.perf_counter()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(10):
            before = time.perf_counter()
            assert (await client.get("/health")).status_code == 200
            baseline.append(time.perf_counter() - before)
            assert baseline[-1] < 0.2

        async def render():
            async with local_runtime.attempt({"mesh": (mesh, "stl")}) as result:
                assert result.outcome == "ok"

        tasks = [
            asyncio.create_task(render()),
            *[asyncio.create_task(get_print_file_analysis(manager, 77, 123, archive_path, None)) for _ in range(100)],
        ]
        try:
            previous = time.perf_counter()
            while not all(task.done() for task in tasks):
                before = time.perf_counter()
                gaps.append(before - previous)
                assert (await client.get("/health")).status_code == 200
                latencies.append(time.perf_counter() - before)
                delivered.clear()
                sent = time.perf_counter()
                await sockets.broadcast({"type": "probe"})
                await asyncio.wait_for(delivered.wait(), 0.2)
                ws_latencies.append(time.perf_counter() - sent)
                process = psutil.Process()
                try:
                    tree = [process, *process.children(recursive=True)]
                    memory.append(sum(p.memory_info().rss for p in tree))
                    for p in tree:
                        times = p.cpu_times()
                        current = times.user + times.system
                        first, _ = cpu.get(p.pid, (current if p.pid == process.pid else 0, current))
                        cpu[p.pid] = (first, current)
                    disks.append(
                        await disk(lambda: sum(p.stat().st_size for p in local_runtime.root.rglob("*") if p.is_file()))
                    )
                except psutil.NoSuchProcess:
                    pass
                previous = before
                await asyncio.sleep(0.02)
            _, *analyses = await asyncio.gather(*tasks)
            assert analyses[0] is not None
            assert all(analysis is analyses[0] for analysis in analyses)
            assert manager._print_file_analysis_contexts[77].state == "ready"
            p95 = statistics.quantiles(latencies, n=20)[18]
            print(
                f"preview + analysis + 100 readers: baseline max={max(baseline):.4f}s, HTTP p95={p95:.4f}s, loop max={max(gaps):.4f}s, WS max={max(ws_latencies):.4f}s, total tree RSS={max(memory)}, disk={max(disks)}, CPU sampled seconds={sum(end - start for start, end in cpu.values()):.2f}, elapsed={time.perf_counter() - started:.2f}s"
            )
            assert p95 < 0.2
            assert max(gaps) < 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            analysis_runtime.runtime = None
            await parser.stop()
            await sockets.shutdown()


def test_async_production_does_not_call_renderers():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "app"
    forbidden = {"generate_stl_thumbnail", "inject_plate_thumbnails_if_missing", "_inject_preview"}
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.AsyncFunctionDef):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        name = getattr(call.func, "id", getattr(call.func, "attr", None))
                        assert name not in forbidden, (path, node.name, name)


@pytest.mark.asyncio
async def test_real_mesh_roundtrip_and_shutdown(tmp_path):
    import trimesh
    from PIL import Image

    mesh = tmp_path / "cube.stl"
    mesh.write_bytes(trimesh.creation.box().export(file_type="stl"))
    service = PreviewRuntime(tmp_path / "private service Україна")
    try:
        await service.start()
        assert service.ready
        pid = service.service.process.pid
        assert pid != os.getpid()
        gaps = []
        running = True

        async def probe():
            loop = asyncio.get_running_loop()
            previous = loop.time()
            while running:
                await asyncio.sleep(0.02)
                current = loop.time()
                gaps.append(current - previous)
                previous = current

        probe_task = asyncio.create_task(probe())
        try:
            async with service.attempt({"mesh": (mesh, "stl")}) as result:
                assert result.outcome == "ok"
                with Image.open(result.files["preview"]) as image:
                    assert image.format == "PNG"
                    assert image.mode == "RGBA"
                    assert max(image.size) <= 256
                    image.save(tmp_path / "verified-cube.png")
            assert max(gaps) < 1
        finally:
            running = False
            await probe_task
    finally:
        await service.stop()
    assert not service.slot.locked()
    assert not list(service.staging.rglob("*.png"))
