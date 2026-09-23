"""Real loopback broker, service, guardian and reused parser child."""

import asyncio
import zipfile

import psutil
import pytest

from backend.app.services import analysis_runtime
from backend.app.services.analysis_runtime import AnalysisRuntime
from backend.app.services.local_worker_broker import LocalWorkerBroker
from backend.app.services.preview_protocol import PreviewError
from backend.app.services.print_file_analysis import AnalysisResourceError, get_print_file_analysis
from backend.app.services.printer_manager import PrinterManager


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
            for child in psutil.Process(runtime.service.process.pid).children()
            if child.name().lower().startswith("python")
        ]
        assert len(children) == 1
        children[0].kill()
        with pytest.raises(AnalysisResourceError, match="analysis service unavailable"):
            await pending
        async with asyncio.timeout(10):
            while not runtime.ready:
                await asyncio.sleep(0.05)
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
