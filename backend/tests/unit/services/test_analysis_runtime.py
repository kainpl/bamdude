"""Real loopback broker, service, guardian and reused parser child."""

import asyncio
import logging
import zipfile

import psutil
import pytest
import pytest_asyncio

from backend.app.services import analysis_runtime
from backend.app.services.analysis_runtime import AnalysisRuntime
from backend.app.services.local_worker_broker import LocalWorkerBroker
from backend.app.services.preview_protocol import PreviewError
from backend.app.services.print_file_analysis import AnalysisResourceError, get_print_file_analysis
from backend.app.services.printer_manager import PrinterManager


@pytest_asyncio.fixture
async def bootstrap_runtime(tmp_path):
    (tmp_path / "archive").mkdir()
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        yield runtime
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["connect", "list", "no_responders", "delete", "delete_after", "create_before", "create_after"]
)
async def test_bootstrap_recovers_transient_failure_with_real_worker(bootstrap_runtime, monkeypatch, caplog, phase):
    from nats.aio.client import Client
    from nats.errors import NoRespondersError, TimeoutError as NatsTimeout
    from nats.js.api import ObjectStoreConfig

    runtime = bootstrap_runtime
    js = runtime.broker.nc.jetstream()
    await js.create_object_store(bucket="bamdude_analysis_old", config=ObjectStoreConfig(max_bytes=1024))
    foreign = await js.create_object_store(bucket="bamdude_preview_survivor", config=ObjectStoreConfig(max_bytes=1024))
    await foreign.put("sentinel", b"do not delete")
    original_request, original_connect = Client.request, Client.connect
    failed = False
    connections = []
    monkeypatch.setattr(analysis_runtime, "_BOOTSTRAP_RETRY_SECONDS", 0.01, raising=False)

    async def connect(client, *args, **kwargs):
        nonlocal failed
        connections.append(client)
        await original_connect(client, *args, **kwargs)
        if phase == "connect" and not failed:
            failed = True
            raise NatsTimeout("private path and token must not be logged")

    async def request(client, subject, *args, **kwargs):
        nonlocal failed
        target = {
            "list": "STREAM.LIST",
            "no_responders": "STREAM.LIST",
            "delete": "STREAM.DELETE.",
            "delete_after": "STREAM.DELETE.",
            "create_before": "STREAM.CREATE.",
            "create_after": "STREAM.CREATE.",
        }.get(phase)
        if client is not runtime.broker.nc and target and target in subject and not failed:
            failed = True
            if phase in ("delete_after", "create_after"):
                await original_request(client, subject, *args, **kwargs)
            if phase == "no_responders":
                raise NoRespondersError
            raise NatsTimeout("private path and token must not be logged")
        return await original_request(client, subject, *args, **kwargs)

    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr(Client, "request", request)
    caplog.set_level(logging.INFO, logger=analysis_runtime.__name__)
    await runtime.start()
    assert failed
    assert runtime.ready, "a transient bootstrap failure must recover in this runtime"
    assert runtime.reason is None and not runtime.closed
    path = runtime.base / "archive" / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")
    assert await runtime.parse(path, 1) is not None
    own = [s.config.name for s in await js.streams_info() if s.config.name.startswith("OBJ_bamdude_analysis_")]
    assert own == [f"OBJ_{runtime.bucket}"]
    assert (await foreign.get("sentinel")).data == b"do not delete"
    assert all(client.is_closed for client in connections if client is not runtime.nc)
    assert "startup_retry" in caplog.text and "startup_ready" in caplog.text
    assert "phase=" in caplog.text and "elapsed_ms=" in caplog.text and runtime.generation in caplog.text
    assert "private path and token" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_bootstrap_failure_is_bounded_and_preserves_reason(bootstrap_runtime, monkeypatch, caplog, permanent):
    from nats.aio.client import Client
    from nats.errors import TimeoutError as NatsTimeout
    from nats.js.errors import APIError

    runtime = bootstrap_runtime
    original = Client.request
    calls = 0
    connections = []
    monkeypatch.setattr(analysis_runtime, "_BOOTSTRAP_RETRY_SECONDS", 0.01, raising=False)

    async def request(client, subject, *args, **kwargs):
        nonlocal calls
        if client is not runtime.broker.nc and "STREAM.LIST" in subject:
            calls += 1
            connections.append(client)
            if permanent:
                raise APIError(code=403, description="secret")
            raise NatsTimeout("secret")
        return await original(client, subject, *args, **kwargs)

    monkeypatch.setattr(Client, "request", request)
    caplog.set_level(logging.WARNING, logger=analysis_runtime.__name__)
    async with asyncio.timeout(5):
        await runtime.start()
    assert calls == (1 if permanent else 3)
    assert not runtime.ready and runtime.reason == "startup_failed"
    assert runtime.monitor is None and runtime.service is None
    assert all(client.is_closed for client in connections)
    assert runtime.broker.nc.is_connected
    assert "phase=stream_list" in caplog.text and "elapsed_ms=" in caplog.text
    assert "secret" not in caplog.text
    await runtime.stop()
    assert runtime.reason == "startup_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "list", "create", "launch"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_shutdown_owns_inflight_bootstrap(bootstrap_runtime, monkeypatch, phase, cancel_caller):
    from nats.aio.client import Client

    runtime = bootstrap_runtime
    entered = asyncio.Event()
    original_connect, original_request, original_launch = Client.connect, Client.request, runtime.launch
    clients = []
    processes = []

    async def hold():
        entered.set()
        await asyncio.Event().wait()

    async def connect(client, *args, **kwargs):
        clients.append(client)
        await original_connect(client, *args, **kwargs)
        if phase == "connect":
            await hold()

    async def request(client, subject, *args, **kwargs):
        result = await original_request(client, subject, *args, **kwargs)
        if client is not runtime.broker.nc and (
            phase == "list" and "STREAM.LIST" in subject or phase == "create" and "STREAM.CREATE." in subject
        ):
            await hold()
        return result

    async def launch():
        await original_launch()
        processes.append(runtime.service.process)
        if phase == "launch":
            await hold()

    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr(Client, "request", request)
    monkeypatch.setattr(runtime, "launch", launch)
    start = asyncio.create_task(runtime.start())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            if cancel_caller:
                start.cancel()
                await asyncio.gather(start, return_exceptions=True)
            else:
                await runtime.stop()
                # The public waiter may still be delivering cancellation, but
                # the owned startup and all of its resources must already end.
                assert runtime.startup.done()
                try:
                    await asyncio.wait_for(asyncio.shield(start), 1)
                except asyncio.CancelledError:
                    pass
        assert not runtime.ready and runtime.closed
        assert runtime.reason == "stopped"
        assert all(client.is_closed for client in clients)
        assert all(process.poll() is not None for process in processes)
        assert runtime.service is None
        assert runtime.monitor is None or runtime.monitor.done()
        assert runtime.broker.nc.is_connected
    finally:
        start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        await runtime.stop()
        # Also close the connection the OLD implementation lost during connect.
        for client in clients:
            await client.close()


@pytest.mark.asyncio
async def test_bootstrap_deadline_bounds_a_stalled_request(bootstrap_runtime, monkeypatch, caplog):
    from nats.aio.client import Client

    runtime = bootstrap_runtime
    original = Client.request
    clients = []
    monkeypatch.setattr(analysis_runtime, "_BOOTSTRAP_SECONDS", 0.1)

    async def request(client, subject, *args, **kwargs):
        if client is not runtime.broker.nc and "STREAM.LIST" in subject:
            clients.append(client)
            await asyncio.Event().wait()
        return await original(client, subject, *args, **kwargs)

    monkeypatch.setattr(Client, "request", request)
    async with asyncio.timeout(3):
        await runtime.start()
    assert len(clients) == 1 and clients[0].is_closed
    assert not runtime.ready and runtime.reason == "startup_failed"
    assert "phase=stream_list error=TimeoutError" in caplog.text
    assert runtime.broker.nc.is_connected


@pytest.mark.asyncio
async def test_bootstrap_admin_request_budget_is_separate_from_control(bootstrap_runtime, monkeypatch):
    from nats.aio.client import Client
    from nats.errors import TimeoutError as NatsTimeout

    runtime = bootstrap_runtime
    original = Client.request
    timeouts = []
    first = True
    monkeypatch.setattr(analysis_runtime, "_BOOTSTRAP_REQUEST_SECONDS", 0.1)
    monkeypatch.setattr(analysis_runtime, "_BOOTSTRAP_RETRY_SECONDS", 0.01)

    async def request(client, subject, *args, **kwargs):
        nonlocal first
        if client is not runtime.broker.nc and subject.startswith("$JS.API."):
            timeouts.append(kwargs["timeout"])
            if first:
                first = False
                # The broker answered; deliver it too late, like a slow read.
                await original(client, subject, *args, **kwargs)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.Event().wait(), kwargs["timeout"])
                raise NatsTimeout
        return await original(client, subject, *args, **kwargs)

    monkeypatch.setattr(Client, "request", request)
    await runtime.start()
    assert runtime.ready
    assert len(timeouts) >= 3 and set(timeouts) == {0.1}
    assert analysis_runtime.CONTROL_SECONDS != 0.1


@pytest.mark.asyncio
async def test_stop_waits_for_retry_cleanup_and_does_not_reconnect(bootstrap_runtime, monkeypatch):
    from nats.aio.client import Client
    from nats.errors import TimeoutError as NatsTimeout

    runtime = bootstrap_runtime
    original_request, original_close = Client.request, Client.close
    closing, release = asyncio.Event(), asyncio.Event()
    failed_client = None
    calls = 0

    async def request(client, subject, *args, **kwargs):
        nonlocal failed_client, calls
        if client is not runtime.broker.nc and "STREAM.LIST" in subject:
            calls += 1
            failed_client = client
            raise NatsTimeout
        return await original_request(client, subject, *args, **kwargs)

    async def close(client):
        if client is failed_client:
            closing.set()
            await release.wait()
        await original_close(client)

    monkeypatch.setattr(Client, "request", request)
    monkeypatch.setattr(Client, "close", close)
    start = asyncio.create_task(runtime.start())
    stops = []
    try:
        async with asyncio.timeout(5):
            await closing.wait()
            stops = [asyncio.create_task(runtime.stop()) for _ in range(2)]
            await asyncio.sleep(0.05)
            assert all(not task.done() for task in stops)
            release.set()
            await asyncio.gather(*stops)
            await asyncio.gather(start, return_exceptions=True)
        assert failed_client.is_closed and calls == 1
        assert runtime.service is None and runtime.monitor is None
        assert not runtime.ready and runtime.closed
    finally:
        release.set()
        start.cancel()
        await asyncio.gather(start, *stops, return_exceptions=True)


@pytest.mark.asyncio
async def test_analysis_cancel_forwards_service_cleanup_diagnostic(tmp_path):
    from backend.app.analysis_service import Service

    service = Service(
        {"generation": "a" * 32, "epoch": "b" * 32, "staging": str(tmp_path), "archive_root": str(tmp_path)}
    )
    run = {"sequence": 1, "attempt_id": "c" * 32, "operation": "run", "source": None}
    service.active = run

    async def running():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"outcome": "canceled", "staging_cleanup": {"path": str(tmp_path), "error": "PermissionError"}}

    service.active_task = asyncio.create_task(running())
    await asyncio.sleep(0)
    reply = await service.command({**run, "operation": "cancel"})
    assert reply["outcome"] == "canceled"
    assert reply["staging_cleanup"]["error"] == "PermissionError"


@pytest.mark.asyncio
async def test_a_failed_parse_keeps_the_workers_reason(tmp_path, monkeypatch):
    """The parent's failure names what the worker answered (local microservice
    rule): "analysis service failed" alone hid a G-code over its size limit."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    path = archive_dir / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        await runtime.start()
        assert runtime.ready, runtime.reason

        async def refused(command, timeout):
            return {"outcome": "parse_failed"}

        monkeypatch.setattr(runtime, "rpc", refused)
        with pytest.raises(AnalysisResourceError, match="analysis service failed: analysis worker parse_failed"):
            await runtime.parse(path, 1)
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_analysis_service_cleanup_failure_preserves_primary_outcome(tmp_path, monkeypatch):
    from backend.app import analysis_service
    from backend.app.services.worker_staging import CleanupResult

    service = analysis_service.Service(
        {"generation": "a" * 32, "epoch": "b" * 32, "staging": str(tmp_path), "archive_root": str(tmp_path)}
    )
    command = {"attempt_id": "c" * 32, "deadline_ns": 1, "source": None}

    async def retire():
        pass

    async def unavailable():
        raise PreviewError("timeout")

    monkeypatch.setattr(service, "retire_child", retire)
    monkeypatch.setattr(service, "start_child", unavailable)
    monkeypatch.setattr(
        analysis_service,
        "cleanup_owned",
        lambda path: CleanupResult("retained_error", path, "PermissionError", 5),
    )
    reply = await service.run(command)
    assert reply["outcome"] == "timeout"
    assert reply["staging_cleanup"]["error"] == "PermissionError"


@pytest.mark.asyncio
async def test_main_cleanup_failure_preserves_parse_and_below_quota_admission(tmp_path, monkeypatch, caplog):
    from backend.app.services.worker_staging import CleanupResult

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    path = archive_dir / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")

    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    await runtime.start()
    original = analysis_runtime.cleanup_owned
    denied = False

    def cleanup(root, **kwargs):
        nonlocal denied
        if not denied and root.parent == runtime.staging / "main":
            denied = True
            return CleanupResult("retained_error", root, "PermissionError", 5)
        return original(root, **kwargs)

    monkeypatch.setattr(analysis_runtime, "cleanup_owned", cleanup)
    try:
        with caplog.at_level(logging.WARNING, logger="backend.app.services.analysis_runtime"):
            first = await runtime.parse(path, 1)
            assert denied and first is not None
            assert "staging_cleanup_failed: phase=main_attempt" in caplog.text
            second = await runtime.parse(path, 1)
            assert second is not None and first.filament_usage == second.filament_usage
            assert "analysis_staging_budget_exceeded" not in caplog.text
    finally:
        monkeypatch.setattr(analysis_runtime, "cleanup_owned", original)
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_final_generation_cleanup_failure_is_logged_without_masking_stop(tmp_path, monkeypatch, caplog):
    from backend.app.services.worker_staging import CleanupResult

    (tmp_path / "archive").mkdir()
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    await runtime.start()
    original = analysis_runtime.cleanup_owned

    def cleanup(path, **kwargs):
        if path == runtime.staging:
            return CleanupResult("retained_error", path, "PermissionError", 5)
        return original(path, **kwargs)

    monkeypatch.setattr(analysis_runtime, "cleanup_owned", cleanup)
    try:
        with caplog.at_level(logging.WARNING, logger="backend.app.services.analysis_runtime"):
            await runtime.stop()
        assert "staging_cleanup_failed: phase=generation" in caplog.text
        assert runtime.staging.exists()
    finally:
        monkeypatch.setattr(analysis_runtime, "cleanup_owned", original)
        if runtime.staging.exists():
            assert original(runtime.staging).status == "removed"
        await broker.stop()


@pytest.mark.asyncio
async def test_retire_hands_off_only_own_service_attempts_after_stop(tmp_path):
    runtime = AnalysisRuntime(tmp_path, None)
    runtime.staging = runtime.root / "staging" / runtime.generation
    service_root = runtime.staging / "service"
    attempt = service_root / ("a" * 32)
    attempt.mkdir(parents=True)
    (attempt / "analysis.bin").write_bytes(b"residue")
    (service_root / "cache").mkdir()
    (service_root / "child.ready").write_bytes(b"12345")

    class Service:
        def __init__(self):
            self.stopped = False

        def stop(self):
            assert attempt.exists()
            self.stopped = True

    service = Service()
    runtime.service = service
    await runtime.retire()
    assert service.stopped and runtime.service is None
    assert not attempt.exists()
    assert (service_root / "cache").exists()
    assert (service_root / "child.ready").exists()


@pytest.mark.asyncio
async def test_failed_retire_keeps_service_handle_and_residue(tmp_path):
    runtime = AnalysisRuntime(tmp_path, None)
    runtime.staging = runtime.root / "staging" / runtime.generation
    attempt = runtime.staging / "service" / ("a" * 32)
    attempt.mkdir(parents=True)

    class Service:
        def stop(self):
            raise RuntimeError("reap unproven")

    service = Service()
    runtime.service = service
    with pytest.raises(RuntimeError, match="reap unproven"):
        await runtime.retire()
    assert runtime.service is service and runtime.uncertain and attempt.exists()


@pytest.mark.asyncio
async def test_post_retire_filesystem_failure_is_not_ownership_uncertain(tmp_path, monkeypatch, caplog):
    from backend.app.services.worker_staging import CleanupResult

    runtime = AnalysisRuntime(tmp_path, None)
    runtime.staging = runtime.root / "staging" / runtime.generation
    attempt = runtime.staging / "service" / ("a" * 32)
    attempt.mkdir(parents=True)
    (attempt / "analysis.bin").write_bytes(b"residue")

    class Service:
        def stop(self):
            assert attempt.exists()

    runtime.service = Service()
    monkeypatch.setattr(
        analysis_runtime,
        "cleanup_owned",
        lambda path, **_kwargs: CleanupResult("retained_error", path, "PermissionError", 5),
    )
    with caplog.at_level(logging.INFO, logger="backend.app.services.analysis_runtime"):
        await runtime.retire()
    assert runtime.service is None and not runtime.uncertain and attempt.exists()
    assert "service_residue_after_retire" in caplog.text
    assert "staging_cleanup_failed: phase=service_residue_after_retire" in caplog.text


@pytest.mark.asyncio
async def test_empty_retired_analysis_skeleton_is_not_warned_on_two_starts(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="backend.app.services.analysis_runtime")
    (tmp_path / "archive").mkdir()
    old = tmp_path / ".cache" / "analysis-service" / "staging" / ("a" * 32)
    (old / "main").mkdir(parents=True)
    (old / "service" / "cache").mkdir(parents=True)
    (old / "service" / "child.ready").write_bytes(b"12345")
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    try:
        for _ in range(2):
            runtime = AnalysisRuntime(tmp_path, broker)
            await runtime.start()
            assert runtime.ready
            await runtime.stop()
    finally:
        await broker.stop()
    assert old.exists()
    assert caplog.text.count("empty_skeleton_count=1") == 2
    assert "Retained analysis staging:" not in caplog.text


@pytest.mark.asyncio
async def test_a_restart_drops_the_previous_generations_bucket_only(tmp_path):
    # Every start names a fresh bucket that reserves its max_bytes against the
    # shared broker's max_file_store, and the store outlives the process: a
    # leftover per restart fills it. Another consumer's bucket must survive.
    from nats.js.api import ObjectStoreConfig

    (tmp_path / "archive").mkdir()
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    try:
        js = broker.nc.jetstream()
        await js.create_object_store(
            bucket="foreign", config=ObjectStoreConfig(bucket="foreign", max_bytes=1024**2, storage="file")
        )
        for _ in range(2):
            runtime = AnalysisRuntime(tmp_path, broker)
            await runtime.start()
            await runtime.stop()
        names = sorted(stream.config.name for stream in await js.streams_info())
        assert names == sorted(["OBJ_foreign", f"OBJ_{runtime.bucket}"])
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_cancel_after_terminal_run_acknowledges_settled_ownership(tmp_path):
    from backend.app.analysis_service import Service

    service = Service(
        {"generation": "generation", "epoch": "epoch", "staging": str(tmp_path), "archive_root": str(tmp_path)}
    )
    run = {"sequence": 7, "attempt_id": "a" * 32, "operation": "run", "source": {"token": "source"}}
    service.terminals[7] = (run, {"outcome": "ok", "artifact": {"name": "old"}})

    assert await service.command({**run, "operation": "cancel"}) == {"outcome": "canceled"}
    assert await service.command({**run, "operation": "cancel", "attempt_id": "b" * 32}) == {
        "outcome": "protocol_error"
    }


def test_service_rejects_non_string_operation_as_protocol_error(tmp_path):
    from backend.app.analysis_service import Service
    from backend.app.services.preview_protocol import encode

    service = Service(
        {"generation": "generation", "epoch": "epoch", "staging": str(tmp_path), "archive_root": str(tmp_path)}
    )
    payload = {
        "version": 1,
        "generation": "generation",
        "epoch": "epoch",
        "attempt_id": "a" * 32,
        "sequence": 1,
        "operation": [],
        "deadline_ns": 1,
        "source": None,
    }
    with pytest.raises(PreviewError, match="protocol_error"):
        service.parse(encode(payload))


@pytest.mark.asyncio
async def test_analysis_service_parses_twice_with_one_child_and_no_input_copy(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    path = archive_dir / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1" />'
            '<filament id="1" used_g="12.5" type="PLA" /></plate></config>',
        )
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nM620 S0\nG1 E2\n")

    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        await runtime.start()
        assert runtime.ready, runtime.reason
        status = await runtime.rpc(runtime.command("status"), 5)
        assert status == {"outcome": "ok", "state": "ready", "attempt_id": None, "epoch": runtime.epoch}
        monkeypatch.setattr(analysis_runtime, "runtime", runtime)
        first = await runtime.parse(path, 1)
        child_pid = (runtime.staging / "service" / "child.ready").read_text(encoding="ascii")
        second = await runtime.parse(path, 1)
        assert first.filament_usage == second.filament_usage
        assert first.filament_usage[0]["used_g"] == 12.5
        transport = runtime.health()["transport"]
        assert transport["attempts"] == transport["ok"] == 2
        assert transport["last_artifact_bytes"] > 0
        assert transport["last_compute_ms"] >= 0
        assert transport["last_transfer_ms"] >= 0
        assert (runtime.staging / "service" / "child.ready").read_text(encoding="ascii") == child_pid
        assert not list((runtime.staging / "main").iterdir())
        manager = PrinterManager()
        cached = await get_print_file_analysis(manager, 7, 42, path, 1)
        assert cached is not None
        assert cached.filament_usage[0]["used_g"] == 12.5
        assert manager._print_file_analysis_contexts[7].worker == "service"
    finally:
        monkeypatch.setattr(analysis_runtime, "runtime", None)
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_cancel_reaps_busy_parser_before_next_request(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    slow = archive_dir / "slow.3mf"
    quick = archive_dir / "quick.3mf"
    with zipfile.ZipFile(slow, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n" * 2_000_000)
    with zipfile.ZipFile(quick, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        await runtime.start()
        assert runtime.ready
        original_pid = (runtime.staging / "service" / "child.ready").read_text(encoding="ascii")
        pending = asyncio.create_task(runtime.parse(slow, 1))
        for _ in range(200):
            if runtime.active_task is pending and list((runtime.staging / "service").glob("*/output.part")):
                break
            if runtime.active_task is pending and list((runtime.staging / "service").glob("[0-9a-f]" * 32)):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("parser request did not start")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        # A cancel that cannot be acknowledged retires the service. Its
        # monitor then relaunches it; admission is intentionally unavailable
        # until that replacement is ready, especially on slower CI hosts.
        async with asyncio.timeout(20):
            while not runtime.ready:
                await asyncio.sleep(0.05)
        assert await runtime.parse(quick, 1) is not None
        assert (runtime.staging / "service" / "child.ready").read_text(encoding="ascii") != original_pid
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_crashed_parser_is_replaced_without_restarting_preview_broker(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    slow = archive_dir / "slow.3mf"
    quick = archive_dir / "quick.3mf"
    with zipfile.ZipFile(slow, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n" * 2_000_000)
    with zipfile.ZipFile(quick, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        await runtime.start()
        assert runtime.ready
        original_pid = int((runtime.staging / "service" / "child.ready").read_text(encoding="ascii"))
        pending = asyncio.create_task(runtime.parse(slow, 1))
        for _ in range(200):
            if list((runtime.staging / "service").glob("[0-9a-f]" * 32)):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("parser request did not start")
        psutil.Process(original_pid).kill()
        with pytest.raises(Exception, match="analysis service"):
            await pending
        assert await runtime.parse(quick, 1) is not None
        assert int((runtime.staging / "service" / "child.ready").read_text(encoding="ascii")) != original_pid
        assert runtime.ready
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_service_death_during_parse_degrades_request_and_recovers(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    slow = archive_dir / "slow.3mf"
    quick = archive_dir / "quick.3mf"
    with zipfile.ZipFile(slow, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n" * 2_000_000)
    with zipfile.ZipFile(quick, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        await runtime.start()
        assert runtime.ready
        pending = asyncio.create_task(runtime.parse(slow, 1))
        async with asyncio.timeout(10):
            while not (runtime.active_task is pending and list((runtime.staging / "service").glob("[0-9a-f]" * 32))):
                await asyncio.sleep(0.01)
        status = await runtime.rpc(runtime.command("status"), 5)
        assert status["state"] == "busy"
        assert status["attempt_id"] is not None
        children = [
            child
            for child in psutil.Process(runtime.service.process.pid).children(recursive=True)
            if "backend.app.analysis_service" in child.cmdline()
        ]
        assert children
        for left in children:
            for right in children:
                if left != right:
                    assert left in right.parents() or right in left.parents()
        max(children, key=lambda child: len(child.parents())).kill()
        with pytest.raises(AnalysisResourceError, match="analysis service unavailable"):
            await pending
        async with asyncio.timeout(10):
            while not runtime.ready:
                await asyncio.sleep(0.05)
        assert not (runtime.staging / "service" / status["attempt_id"]).exists()
        assert await runtime.parse(quick, 1) is not None
        assert runtime.stats["failed"] >= 1
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_shared_broker_stops_only_after_both_services(tmp_path):
    from backend.app.services import analysis_runtime, preview_runtime
    from backend.app.services.local_worker_broker import get_local_worker_broker, stop_local_worker_broker

    (tmp_path / "archive").mkdir()
    await preview_runtime.start_preview_runtime(tmp_path)
    broker = get_local_worker_broker()
    assert broker is not None
    assert preview_runtime.runtime.ready
    await analysis_runtime.start_analysis_runtime(tmp_path)
    try:
        assert analysis_runtime.runtime.ready
        assert get_local_worker_broker() is broker
        await preview_runtime.stop_preview_runtime()
        assert analysis_runtime.runtime.ready
        assert broker.nc.is_connected
    finally:
        await analysis_runtime.stop_analysis_runtime()
        await preview_runtime.stop_preview_runtime()
        await stop_local_worker_broker()
    assert get_local_worker_broker() is None


@pytest.mark.asyncio
async def test_initial_service_failure_retries_without_restarting_application(tmp_path, monkeypatch):
    (tmp_path / "archive").mkdir()
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    original_launch = runtime.launch
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PreviewError("unavailable")
        await original_launch()

    monkeypatch.setattr(runtime, "launch", fail_once)
    try:
        await runtime.start()
        assert not runtime.ready
        async with asyncio.timeout(8):
            while not runtime.ready:
                await asyncio.sleep(0.05)
        assert attempts == 2
        assert runtime.health()["state"] == "ready"
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_idle_parser_death_is_reaped_and_next_parse_uses_fresh_child(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    path = archive_dir / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nG1 E2\n")
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AnalysisRuntime(tmp_path, broker)
    try:
        await runtime.start()
        assert runtime.ready
        original_pid = int((runtime.staging / "service" / "child.ready").read_text(encoding="ascii"))
        psutil.Process(original_pid).kill()
        await asyncio.sleep(0.7)  # let the idle watchdog observe and reap it
        assert await runtime.parse(path, 1) is not None
        current_pid = int((runtime.staging / "service" / "child.ready").read_text(encoding="ascii"))
        assert current_pid != original_pid
        assert not psutil.pid_exists(original_pid)
    finally:
        await runtime.stop()
        await broker.stop()
