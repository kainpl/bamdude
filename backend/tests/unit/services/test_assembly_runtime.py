"""The assembly microservice end to end: real loopback broker, real service, real guardian, real packing child."""

import asyncio
import logging
import time

import psutil
import pytest
import pytest_asyncio

from backend.app.services.assembly import runtime as assembly_runtime
from backend.app.services.assembly.runtime import AssemblyFailed, AssemblyRuntime
from backend.app.services.local_worker_broker import LocalWorkerBroker
from backend.tests.unit.test_assembly_child import _printer, stage

SLOW = {"rotation_step": 45, "spacing": 0.0}  # with a filled plate: seconds of packing


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly_runtime, "_POLL_SECONDS", 0.1)
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AssemblyRuntime(tmp_path, broker)
    await runtime.start()
    assert runtime.ready
    try:
        yield runtime
    finally:
        await runtime.stop()
        await broker.stop()


def _job(runtime, name, parts, **kwargs):
    return stage(runtime.staging / name, parts, [_printer("p0", 256)], **kwargs)


def _slow(runtime, name):
    return _job(runtime, name, [("lid", "fill")], layout=SLOW)


def _children(runtime):
    return psutil.Process(runtime.service.process.pid).children(recursive=True)


def _packing_children(runtime):
    found = []
    for process in _children(runtime):
        try:
            if "backend.app.assembly_child" in process.cmdline():
                found.append(process)
        except psutil.Error:
            continue
    return found


async def _until(condition, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_a_real_job_packs_and_the_parent_logs_start_and_outcome(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=assembly_runtime.__name__)
    monkeypatch.setattr(assembly_runtime, "_POLL_SECONDS", 0.1)
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AssemblyRuntime(tmp_path, broker)
    try:
        await runtime.start()
        root = _job(runtime, "a", [("frame", 1), ("lid", 3)])
        result = await runtime.run(root, job_id=7)
        assert result["outcome"] == "ok" and result["printers"][0]["status"] == "ok"
        assert (root / result["printers"][0]["files"][0]["member"]).is_file()
    finally:
        await runtime.stop()
        await broker.stop()
    lines = [record.getMessage() for record in caplog.records]
    assert sum("Assembly service ready" in line for line in lines) == 1
    assert sum(line.startswith("Assembly job=7 ") and line.endswith(" start") for line in lines) == 1
    outcomes = [line for line in lines if line.startswith("Assembly job=7 ") and "outcome=" in line]
    assert len(outcomes) == 1 and "outcome=ok" in outcomes[0] and "elapsed_ms=" in outcomes[0]
    assert str(tmp_path) not in "\n".join(line for line in lines if "job=7" in line)
    assert runtime.health()["jobs"]["ok"] == 1 and runtime.quiesced()


@pytest.mark.asyncio
async def test_progress_reaches_the_callback(runtime):
    seen = []
    await runtime.run(_slow(runtime, "a"), job_id=1, on_progress=seen.append)
    packing = [p for p in seen if p["stage"] == "packing"]
    assert packing and any(p.get("placed", 0) > 0 and p.get("printer_count") == 1 for p in packing)
    assert not any(p["stage"] == "waiting" for p in seen), "next in line is not waiting behind anyone"
    assert any(p.get("fill_part") == 0 for p in packing), "a filled plate says which part it is filling"


@pytest.mark.asyncio
async def test_finish_search_reaches_the_real_child_without_cancelling(runtime):
    seen = []
    root = _job(runtime, "finish-search", [("lid", "fill")], layout={"long_search": True, "rotation_step": -1})
    result = await runtime.run(
        root,
        job_id=91,
        on_progress=seen.append,
        finish_requested=lambda: any(p.get("search_best_plates", 0) for p in seen),
    )
    assert result["outcome"] == "ok" and result["printers"][0]["status"] == "ok"
    assert (root / "finish").is_file()
    assert result["printers"][0]["fill_counts"]["0"] > 0
    assert not _packing_children(runtime)


@pytest.mark.asyncio
async def test_a_second_job_waits_in_the_service_and_main_stays_free(runtime):
    first_seen, second_seen = [], []
    first = asyncio.create_task(runtime.run(_slow(runtime, "a"), job_id=1, on_progress=first_seen.append))
    assert await _until(lambda: any(p["stage"] == "packing" for p in first_seen))
    second = asyncio.create_task(
        runtime.run(_job(runtime, "b", [("lid", 2)]), job_id=2, on_progress=second_seen.append)
    )
    assert await _until(lambda: bool(second_seen))
    assert second_seen[0] == {"stage": "waiting", "position": 1}
    ticks = 0
    while not second.done():  # the loop keeps answering while two jobs are in the service
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks > 20
    assert (await first)["outcome"] == "ok" and (await second)["outcome"] == "ok"
    assert any(p["stage"] == "packing" for p in second_seen)


@pytest.mark.asyncio
async def test_timeout_kills_the_child_and_says_so(runtime, caplog):
    caplog.set_level(logging.INFO, logger=assembly_runtime.__name__)
    with pytest.raises(AssemblyFailed) as failed:
        await runtime.run(_slow(runtime, "a"), job_id=3, timeout_s=0.5)
    assert failed.value.outcome == "timeout"
    assert "Assembly job=3" in caplog.text and "outcome=timeout" in caplog.text
    assert await _until(lambda: not _packing_children(runtime))
    assert not list((runtime.staging / "a" / "out").glob("*.3mf"))
    assert (await runtime.run(_job(runtime, "b", [("lid", 1)]), job_id=4))["outcome"] == "ok"


@pytest.mark.asyncio
async def test_cancelling_the_task_kills_the_child(runtime, caplog):
    caplog.set_level(logging.INFO, logger=assembly_runtime.__name__)
    seen = []
    task = asyncio.create_task(runtime.run(_slow(runtime, "a"), job_id=5, on_progress=seen.append))
    assert await _until(lambda: any(p["stage"] == "packing" for p in seen))
    assert await _until(lambda: bool(_packing_children(runtime)))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _packing_children(runtime), "the cancel is answered only after the child is gone"
    assert "Assembly job=5" in caplog.text and "outcome=canceled" in caplog.text
    assert runtime.quiesced() and runtime.ready
    assert (await runtime.run(_job(runtime, "b", [("lid", 1)]), job_id=6))["outcome"] == "ok"


@pytest.mark.asyncio
async def test_a_queued_job_can_be_cancelled_without_touching_the_running_one(runtime):
    first_seen, second_seen = [], []
    first = asyncio.create_task(runtime.run(_slow(runtime, "a"), job_id=1, on_progress=first_seen.append))
    assert await _until(lambda: any(p["stage"] == "packing" for p in first_seen))
    second = asyncio.create_task(runtime.run(_slow(runtime, "b"), job_id=2, on_progress=second_seen.append))
    assert await _until(lambda: bool(second_seen))
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert (await first)["outcome"] == "ok"
    assert not (runtime.staging / "b" / "result.json").exists()


@pytest.mark.asyncio
async def test_garbage_job_is_failed_not_a_crash(runtime):
    root = runtime.staging / "a"
    root.mkdir()
    (root / "job.json").write_bytes(b"{not json")
    with pytest.raises(AssemblyFailed) as failed:
        await runtime.run(root, job_id=8)
    assert (failed.value.outcome, failed.value.why) == ("failed", "bad_job")
    assert runtime.ready


@pytest.mark.asyncio
async def test_an_unreadable_mesh_names_its_part(runtime):
    root = _job(runtime, "a", [("lid", 1)], raw=b"not a mesh at all")
    with pytest.raises(AssemblyFailed) as failed:
        await runtime.run(root, job_id=9)
    assert failed.value.outcome == "failed" and failed.value.why == "unreadable_mesh"
    assert failed.value.parts == [{"index": 0, "name": "lid", "reason": "unreadable_mesh"}]


@pytest.mark.asyncio
async def test_a_directory_outside_the_staging_is_refused(runtime, tmp_path):
    outside = stage(tmp_path / "elsewhere", [("lid", 1)], [_printer("p0", 256)])
    with pytest.raises(AssemblyFailed) as failed:
        await runtime.run(outside, job_id=10)
    assert failed.value.outcome == "unavailable"
    assert not (outside / "result.json").exists()


@pytest.mark.asyncio
async def test_a_dead_service_is_relaunched_and_the_job_is_unavailable(runtime, caplog):
    caplog.set_level(logging.INFO, logger=assembly_runtime.__name__)
    seen = []
    task = asyncio.create_task(runtime.run(_slow(runtime, "a"), job_id=11, on_progress=seen.append))
    assert await _until(lambda: any(p["stage"] == "packing" for p in seen))
    old_epoch = runtime.epoch
    for process in [*_children(runtime), psutil.Process(runtime.service.process.pid)]:
        try:
            process.kill()
        except psutil.Error:
            pass
    with pytest.raises(AssemblyFailed) as failed:
        await task
    assert failed.value.outcome == "unavailable"
    assert await _until(lambda: runtime.ready and runtime.epoch != old_epoch, seconds=30)
    assert "Assembly service lost" in caplog.text and "Assembly service restarted" in caplog.text
    assert "outcome=unavailable" in caplog.text
    assert (await runtime.run(_job(runtime, "b", [("lid", 1)]), job_id=12))["outcome"] == "ok"


@pytest.mark.asyncio
async def test_stop_drains_a_running_job(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly_runtime, "_POLL_SECONDS", 0.1)
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AssemblyRuntime(tmp_path, broker)
    try:
        await runtime.start()
        seen = []
        task = asyncio.create_task(runtime.run(_slow(runtime, "a"), job_id=13, on_progress=seen.append))
        assert await _until(lambda: any(p["stage"] == "packing" for p in seen))
        service = psutil.Process(runtime.service.process.pid)
        tree = [service, *service.children(recursive=True)]
        await runtime.stop()
        assert task.done() and task.cancelled()
        assert not any(process.is_running() and process.status() != psutil.STATUS_ZOMBIE for process in tree)
        assert runtime.quiesced() and not runtime.staging.exists()
        with pytest.raises(AssemblyFailed):
            await runtime.run(tmp_path, job_id=14)
    finally:
        await runtime.stop()
        await broker.stop()


@pytest.mark.asyncio
async def test_a_few_unanswered_status_requests_do_not_lose_the_job(runtime, monkeypatch):
    real, failed = runtime.rpc, []

    async def flaky(command, timeout=2):
        if command["operation"] == "status" and len(failed) < 3:
            failed.append(command["attempt_id"])
            raise TimeoutError
        return await real(command, timeout)

    monkeypatch.setattr(runtime, "rpc", flaky)
    result = await runtime.run(_slow(runtime, "a"), job_id=21)
    assert result["outcome"] == "ok" and len(failed) == 3 and runtime.ready


@pytest.mark.asyncio
async def test_a_service_that_stops_answering_is_given_up_after_the_limit(runtime, monkeypatch):
    monkeypatch.setattr(assembly_runtime, "_POLL_FAILURES", 2)
    real, asked = runtime.rpc, []

    async def silent(command, timeout=2):
        if command["operation"] == "status":
            asked.append(1)
            raise TimeoutError
        return await real(command, timeout)

    monkeypatch.setattr(runtime, "rpc", silent)
    with pytest.raises(AssemblyFailed) as failed:
        await runtime.run(_slow(runtime, "a"), job_id=22)
    assert failed.value.outcome == "unavailable" and len(asked) == 2
    assert await _until(lambda: not _packing_children(runtime))


@pytest.mark.requires_cad
@pytest.mark.asyncio
async def test_a_step_part_is_converted_at_print_precision_and_packed(tmp_path, monkeypatch):
    """Real broker, service, converter and packer: a STEP source becomes an object of the project."""
    import json
    import shutil
    from pathlib import Path

    from platenest.bambu3mf import read_project

    from backend.tests.cad_lane import cad_app_dir

    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "step" / "occt" / "plate_holes.step"
    monkeypatch.setattr(assembly_runtime, "_POLL_SECONDS", 0.1)
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = AssemblyRuntime(tmp_path, broker, app_dir=cad_app_dir())
    seen = []
    try:
        await runtime.start()
        root = _job(runtime, "a", [("lid", 2), ("lid", 1)])
        # part 1 is a STEP file: staged for the converter, named to the packer as the mesh it will leave
        (root / "src" / "1.stl").unlink()
        (root / "step" / "1").mkdir(parents=True)
        shutil.copyfile(fixture, root / "step" / "1" / "input.step")
        job = json.loads((root / "job.json").read_text(encoding="utf-8"))
        job["sources"][1] = {"member": "step/1/mesh.stl", "format": "stl"}
        job["parts"][1]["name"] = "plate_holes.step"
        (root / "job.json").write_text(json.dumps(job), encoding="utf-8")

        result = await runtime.run(root, job_id=3, on_progress=seen.append)
        assert result["outcome"] == "ok" and result["printers"][0]["status"] == "ok"
        project = read_project((root / result["printers"][0]["files"][0]["member"]).read_bytes())
        assert [obj.name for obj in project.objects] == ["lid", "plate_holes.step"]
        assert len(project.objects[1].triangles) > 500, "the holes are round at the slicer's precision"
        assert not (root / "step" / "1" / "input.step").exists()
        left = [" ".join(process.cmdline()) for process in _children(runtime)]
        assert not [line for line in left if "step_thumbnail" in line or "assembly_child" in line]
    finally:
        await runtime.stop()
        await broker.stop()
    assert {"stage": "converting", "convert": 1, "convert_count": 1} in seen


def test_staging_of_a_main_that_is_gone_is_removed_and_anything_else_is_only_reported(tmp_path, caplog):
    """What a dead main staged is of no use to anyone: only that main would have published it."""
    import json
    import os

    runtime = AssemblyRuntime(tmp_path, None)
    parent = runtime.staging.parent
    parent.mkdir(parents=True)
    me = psutil.Process(os.getpid())
    owners = {
        "dead": {"pid": 2**22 + 12345, "started": 1.0},
        "reused": {"pid": me.pid, "started": me.create_time() - 5000.0},  # the pid lives on as another process
        "alive": {"pid": me.pid, "started": me.create_time()},
    }
    for name, owner in owners.items():
        (parent / name / "job").mkdir(parents=True)
        (parent / f"{name}.owner").write_text(json.dumps(owner))
    (parent / "unknown" / "job").mkdir(parents=True)
    (parent / "orphan.owner").write_text("{}")
    caplog.set_level(logging.INFO, logger=assembly_runtime.__name__)
    runtime._sweep_retained()
    assert sorted(path.name for path in parent.iterdir()) == ["alive", "alive.owner", "unknown"]
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "removed=2" in text and "count=2" in text


@pytest.mark.asyncio
async def test_a_started_runtime_records_who_owns_its_staging(runtime):
    import json
    import os

    record = json.loads((runtime.staging.parent / f"{runtime.generation}.owner").read_text())
    assert record["pid"] == os.getpid() and abs(record["started"] - psutil.Process().create_time()) < 1.0
    assert not list(runtime.staging.iterdir())


def test_an_owner_record_that_names_no_process_never_stops_the_service_from_starting(tmp_path):
    import json

    runtime = AssemblyRuntime(tmp_path, None)
    parent = runtime.staging.parent
    for name, pid in (("negative", -1), ("huge", 2**70)):
        (parent / name).mkdir(parents=True)
        (parent / f"{name}.owner").write_text(json.dumps({"pid": pid, "started": 0}))
    runtime._sweep_retained()
    # a pid no system can hold names a process that is gone; one that is no pid at all proves nothing
    assert (parent / "negative").is_dir() and not (parent / "huge").exists()
