"""The assembly service's own rules, without a broker or a child: the wire, the queue, the files it reads."""

import asyncio
import json
import uuid

import pytest

from backend.app import assembly_service
from backend.app.assembly_service import QUEUE_LIMIT, Service, _read_progress, _read_result
from backend.app.services.preview_protocol import PreviewError, encode


@pytest.fixture
def service(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    return Service(
        {"generation": "g" * 32, "epoch": "e" * 32, "staging": str(staging), "cache": str(tmp_path / "cache")}
    )


def _command(service, operation, attempt=None, **changes):
    command = {
        "version": 1,
        "generation": service.config["generation"],
        "epoch": service.config["epoch"],
        "attempt_id": attempt or uuid.uuid4().hex,
        "operation": operation,
        "root": None,
        "budget_ns": None,
    }
    command.update(changes)
    return command


def _run(service, name="a", **changes):
    root = service.staging / name
    root.mkdir(exist_ok=True)
    return {**_command(service, "run"), "root": str(root), "budget_ns": 10**9, **changes}


def test_limits_are_the_agreed_ones():
    assert QUEUE_LIMIT == 16
    assert assembly_service.MEMORY_BYTES == assembly_service.STAGING_BYTES == 2 * 1024**3


def test_a_run_names_a_directory_inside_the_staging(service, tmp_path):
    parsed = service.parse(encode(_run(service)))
    assert parsed["root"] == (service.staging / "a").resolve()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for root in (str(outside), str(service.staging), str(service.staging / "missing"), str(service.staging / "..")):
        with pytest.raises(PreviewError):
            service.parse(encode(_command(service, "run", root=root, budget_ns=10**9)))


@pytest.mark.parametrize(
    "changes",
    [
        {"epoch": "f" * 32},
        {"generation": "f" * 32},
        {"version": 2},
        {"attempt_id": "../x"},
        {"operation": "exec"},
        {"budget_ns": 0},
        {"budget_ns": 10**18},
        {"budget_ns": 1.5},
    ],
)
def test_a_malformed_command_is_a_protocol_error(service, changes):
    with pytest.raises(PreviewError):
        service.parse(encode(_run(service, **changes)))


def test_a_status_carries_no_root(service):
    with pytest.raises(PreviewError):
        service.parse(encode(_command(service, "status", root=str(service.staging))))
    with pytest.raises(PreviewError):
        service.parse(encode({**_command(service, "status"), "extra": 1}))


@pytest.mark.asyncio
async def test_the_queue_is_bounded_and_positions_are_told(service):
    commands = [service.parse(encode(_run(service, f"j{n}"))) for n in range(QUEUE_LIMIT + 1)]
    replies = [await service.command(command) for command in commands]
    assert [reply["outcome"] for reply in replies] == ["ok"] * QUEUE_LIMIT + ["busy"]
    assert [reply["position"] for reply in replies[:QUEUE_LIMIT]] == list(range(QUEUE_LIMIT))
    again = await service.command(commands[3])
    assert again == {"outcome": "ok", "state": "queued", "position": 3}, "the same request is not queued twice"
    assert len(service.queue) == QUEUE_LIMIT


@pytest.mark.asyncio
async def test_cancelling_a_queued_job_frees_its_place(service):
    first, second = (service.parse(encode(_run(service, name))) for name in ("a", "b"))
    await service.command(first)
    await service.command(second)
    cancel = service.parse(encode(_command(service, "cancel", first["attempt_id"])))
    assert await service.command(cancel) == {"outcome": "canceled"}
    status = await service.command(service.parse(encode(_command(service, "status", first["attempt_id"]))))
    assert status["state"] == "done" and status["result"]["outcome"] == "canceled"
    status = await service.command(service.parse(encode(_command(service, "status", second["attempt_id"]))))
    assert status == {"outcome": "ok", "state": "queued", "position": 0}
    unknown = await service.command(service.parse(encode(_command(service, "status"))))
    assert unknown == {"outcome": "ok", "state": "unknown"}


@pytest.mark.asyncio
async def test_an_uncertain_service_takes_no_job(service):
    service.uncertain = True
    assert await service.command(service.parse(encode(_run(service)))) == {"outcome": "unavailable"}
    ready = await service.command(service.parse(encode(_command(service, "ready"))))
    assert ready["outcome"] == "unavailable"


def test_progress_is_reduced_to_whole_numbers(tmp_path):
    assert _read_progress(tmp_path) is None
    (tmp_path / "progress.json").write_text(
        json.dumps(
            {"printer": "p0", "printer_index": 0, "printer_count": 2, "placed": 5, "total": 9, "fill_part": None}
        )
    )
    assert _read_progress(tmp_path) == {"printer_index": 0, "printer_count": 2, "placed": 5, "total": 9}
    for bad in ('{"placed": "5"}', '{"placed": -1}', '{"placed": true}', "[1]", "{", "x" * 5000):
        (tmp_path / "progress.json").write_text(bad)
        assert _read_progress(tmp_path) is None


def test_the_result_file_is_trusted_for_two_words_only(tmp_path):
    assert _read_result(tmp_path) == ("failed", "no_result")
    for content, expected in [
        ('{"outcome": "ok", "why": ""}', ("ok", "")),
        ('{"outcome": "failed", "why": "mesh_too_large"}', ("failed", "mesh_too_large")),
        ('{"outcome": "failed", "why": "C:/secret/path.stl"}', ("failed", "")),
        ('{"outcome": "published"}', ("failed", "bad_result")),
        ("[]", ("failed", "bad_result")),
        ("{", ("failed", "bad_result")),
        (" " * (256 * 1024 + 1), ("failed", "result_too_large")),
    ]:
        (tmp_path / "result.json").write_text(content)
        assert _read_result(tmp_path) == expected


def test_the_guardian_allows_both_assembly_modules():
    from backend.app.worker_guardian import _PREVIEW_MODULES

    assert {"backend.app.assembly_service", "backend.app.assembly_child"} <= _PREVIEW_MODULES


# --- STEP sources: converted here, one disposable converter per file, before the packing child ---


def _step_job(service, parts=(0,), name="s"):
    root = service.staging / name
    for part in parts:
        (root / "step" / str(part)).mkdir(parents=True)
        (root / "step" / str(part) / "input.step").write_bytes(b"ISO-10303-21;")
    return assembly_service.Job(uuid.uuid4().hex, root, 60 * 10**9)


def _fake_children(monkeypatch, behave):
    """Replace the process class: ``behave(module, payload)`` plays the child; spawns and stops are recorded."""
    from types import SimpleNamespace

    events = []

    class Child:
        def __init__(self, module, payload, cache, job_memory_limit=None):
            events.append(("spawn", module))
            self.module = module
            behave(module, payload)
            self.process = SimpleNamespace(poll=lambda: 0)

        def rss(self):
            return 0

        def stop(self):
            events.append(("stop", self.module))

    monkeypatch.setattr(assembly_service, "PreviewProcess", Child)
    monkeypatch.setattr(assembly_service, "_cad_site", lambda app_dir: "site")
    return events


def _converted(payload, triangles=2, **changes):
    from pathlib import Path

    root = Path(payload["root"])
    (root / "mesh.stl").write_bytes(b"\0" * (84 + 50 * triangles))
    (root / "result.json").write_text(json.dumps({"outcome": "ok", "triangles": triangles, **changes}))


def test_step_sources_are_the_numbered_directories_that_hold_an_input(service):
    job = _step_job(service, parts=(2, 0, 10))
    (job.root / "step" / "x").mkdir()
    (job.root / "step" / "01").mkdir()
    (job.root / "step" / "5").mkdir()  # no input: not a source
    assert assembly_service._step_parts(job.root) == [0, 2, 10]
    assert assembly_service._step_parts(service.staging) == []


@pytest.mark.parametrize(
    ("result", "size", "expected"),
    [
        ({"outcome": "ok", "triangles": 2}, 184, ""),
        ({"outcome": "ok", "triangles": 2}, 134, "step_failed"),  # the mesh is not the one the result names
        ({"outcome": "ok", "triangles": 2}, None, "step_failed"),
        ({"outcome": "ok", "triangles": 3_000_000}, 84 + 150_000_000, "step_failed"),
        ({"outcome": "ok", "triangles": True}, 134, "step_failed"),
        ({"outcome": "resource_limit", "why": "triangle_cap"}, None, "step_too_large"),
        ({"outcome": "cad_unavailable", "why": "bundle_import"}, None, "step_unavailable"),
        ({"outcome": "render_failed", "why": "not_solid"}, None, "step_failed"),
        ({"outcome": "whatever"}, None, "step_failed"),
        (None, None, "step_failed"),
    ],
)
def test_a_conversion_is_taken_only_when_its_mesh_is_the_one_it_names(tmp_path, result, size, expected):
    if result is not None:
        (tmp_path / "result.json").write_text(json.dumps(result))
    if size is not None:
        with (tmp_path / "mesh.stl").open("wb") as mesh:
            mesh.truncate(size)
    assert assembly_service._read_conversion(tmp_path) == expected


@pytest.mark.asyncio
async def test_step_files_are_converted_one_by_one_before_the_packer_starts(service, monkeypatch):
    def behave(module, payload):
        if module == assembly_service.STEP_MODULE:
            assert payload["mode"] == "print" and payload["site"] == "site"
            _converted(payload)
        else:
            (job.root / "result.json").write_text(json.dumps({"outcome": "ok", "why": ""}))

    events = _fake_children(monkeypatch, behave)
    job = _step_job(service, parts=(0, 3))
    await service.execute(job)
    converter, packer = assembly_service.STEP_MODULE, "backend.app.assembly_child"
    assert events == [
        ("spawn", converter), ("stop", converter), ("spawn", converter), ("stop", converter),
        ("spawn", packer), ("stop", packer),
    ]  # fmt: skip
    assert job.result["outcome"] == "ok"
    for part in (0, 3):
        left = sorted(path.name for path in (job.root / "step" / str(part)).iterdir())
        assert left == ["mesh.stl"], "the packer gets the mesh alone"


@pytest.mark.asyncio
async def test_a_failed_conversion_names_its_part_and_nothing_is_packed(service, monkeypatch):
    def behave(module, payload):
        root = payload["root"]
        if root.endswith("0"):
            _converted(payload)
        else:
            with open(f"{root}/result.json", "w") as out:
                json.dump({"outcome": "resource_limit", "why": "triangle_cap"}, out)

    events = _fake_children(monkeypatch, behave)
    job = _step_job(service, parts=(0, 4, 7))
    await service.execute(job)
    assert [module for kind, module in events if kind == "spawn"] == [assembly_service.STEP_MODULE] * 2
    assert (job.result["outcome"], job.result["why"]) == ("failed", "step_too_large")
    written = json.loads((job.root / "result.json").read_text())
    assert written == {
        "outcome": "failed",
        "why": "step_too_large",
        "parts": [{"index": 4, "reason": "step_too_large"}],
    }


@pytest.mark.asyncio
async def test_without_the_cad_bundle_a_step_job_fails_by_name_and_starts_nothing(service, monkeypatch):
    events = _fake_children(monkeypatch, lambda module, payload: None)
    monkeypatch.setattr(assembly_service, "_cad_site", lambda app_dir: None)
    job = _step_job(service)
    await service.execute(job)
    assert events == [] and job.result["why"] == "step_unavailable"
    assert json.loads((job.root / "result.json").read_text())["parts"] == [{"index": 0, "reason": "step_unavailable"}]


@pytest.mark.asyncio
async def test_the_conversion_tells_which_file_it_is_on(service, monkeypatch):
    seen = []

    def behave(module, payload):
        seen.append(dict(job.progress or {}))
        if module == assembly_service.STEP_MODULE:
            _converted(payload)
        else:
            (job.root / "result.json").write_text(json.dumps({"outcome": "ok", "why": ""}))

    _fake_children(monkeypatch, behave)
    job = _step_job(service, parts=(1, 2))
    await service.execute(job)
    assert seen == [{"convert": 1, "convert_count": 2}, {"convert": 2, "convert_count": 2}, {}]


def _live_children(monkeypatch, on_stop=None):
    """Children that run until they are stopped — what a real converter or packer looks like to the watch loop."""
    from types import SimpleNamespace

    events = []

    class Child:
        def __init__(self, module, payload, cache, job_memory_limit=None):
            events.append(("spawn", module))
            self.module, self.code = module, None
            self.process = SimpleNamespace(poll=lambda: self.code)

        def rss(self):
            return 0

        def stop(self):
            if on_stop:
                on_stop(self)
            self.code = 1
            events.append(("stop", self.module))

    monkeypatch.setattr(assembly_service, "PreviewProcess", Child)
    monkeypatch.setattr(assembly_service, "_cad_site", lambda app_dir: "site")
    return events


async def _spawned(events, count=1):
    for _ in range(400):
        if sum(kind == "spawn" for kind, _ in events) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no child was started")


@pytest.mark.asyncio
async def test_a_stop_during_a_conversion_ends_the_job_and_starts_nothing_else(service, monkeypatch):
    events = _live_children(monkeypatch)
    job = _step_job(service, parts=(0, 1))
    job.task = asyncio.create_task(service.execute(job))
    await _spawned(events)
    job.task.cancel()
    await asyncio.gather(job.task, return_exceptions=True)
    assert events == [("spawn", assembly_service.STEP_MODULE), ("stop", assembly_service.STEP_MODULE)]
    assert (job.result["outcome"], job.result["why"]) == ("canceled", "cancel")
    assert not service.uncertain


@pytest.mark.asyncio
async def test_a_stop_that_lands_while_a_finished_converter_is_being_reaped_is_not_lost(service, monkeypatch):
    """The reaping waits through a cancellation on purpose; the stop must still end the job afterwards."""
    import threading

    reaping, release = threading.Event(), threading.Event()

    def on_stop(child):
        reaping.set()
        release.wait(5)

    events = _fake_children(monkeypatch, lambda module, payload: _converted(payload))
    original = assembly_service.PreviewProcess

    class Slow(original):
        def stop(self):
            on_stop(self)
            super().stop()

    monkeypatch.setattr(assembly_service, "PreviewProcess", Slow)
    job = _step_job(service, parts=(0, 1))
    job.task = asyncio.create_task(service.execute(job))
    assert await asyncio.to_thread(reaping.wait, 5)
    job.task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(job.task, return_exceptions=True)
    assert [kind for kind, _ in events] == ["spawn", "stop"], "the second file and the packer never start"
    assert (job.result["outcome"], job.result["why"]) == ("canceled", "cancel")


@pytest.mark.asyncio
async def test_a_conversion_past_the_deadline_is_stopped_as_a_timeout(service, monkeypatch):
    events = _live_children(monkeypatch)
    monkeypatch.setattr(assembly_service, "_GRACE_NS", 0)
    job = _step_job(service)
    job.budget_ns = 10**8
    await service.execute(job)
    assert events == [("spawn", assembly_service.STEP_MODULE), ("stop", assembly_service.STEP_MODULE)]
    assert (job.result["outcome"], job.result["why"]) == ("timeout", "deadline")


@pytest.mark.asyncio
async def test_a_converter_stopped_for_memory_names_its_part_as_too_large(service, monkeypatch):
    events = _live_children(monkeypatch)
    monkeypatch.setattr(assembly_service, "_WATCH_SECONDS", 0.0)
    monkeypatch.setattr(assembly_service, "MEMORY_BYTES", -1)
    job = _step_job(service, parts=(3,))
    await service.execute(job)
    assert [kind for kind, _ in events] == ["spawn", "stop"]
    assert (job.result["outcome"], job.result["why"]) == ("failed", "step_too_large")
    assert json.loads((job.root / "result.json").read_text())["parts"] == [{"index": 3, "reason": "step_too_large"}]
