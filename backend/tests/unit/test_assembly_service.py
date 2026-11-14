"""The assembly service's own rules, without a broker or a child: the wire, the queue, the files it reads."""

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
