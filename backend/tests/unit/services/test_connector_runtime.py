"""The connector runtime owns its work through restore and shutdown (OrcaBamDude spec r2 §8.3-8.4, §12).

- Admission closes before the restore's first await, and the drain answers False while owned work still runs.
- An upload slot is held until the work using the bytes has ended, even when the request gave up.
- Coordinators are bounded per key identity and per process; a replay of a running operation reuses its task.
- A restore's fence survives a crash: the next start rotates the storage epoch before admission opens, and keeps
  admission closed while it cannot. A restore that replaced nothing leaves the epoch alone.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.services import connector_runtime as runtime_module, connector_submissions as submissions
from backend.app.services.connector_runtime import ConnectorRuntime
from backend.app.services.connector_submissions import ConnectorActor, ConnectorRefusal

pytestmark = pytest.mark.asyncio


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.settings, "base_dir", tmp_path)
    return ConnectorRuntime()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def _actor(identity="a" * 8) -> ConnectorActor:
    return ConnectorActor(key_id=1, key_identity=identity, owner_id=None)


def _blocking_run(monkeypatch, gate: asyncio.Event):
    started = []

    async def run(submission_id, actor, *, states):
        started.append(submission_id)
        await gate.wait()

    monkeypatch.setattr(submissions, "run", run)
    return started


async def test_a_restore_closes_admission_before_its_first_await(runtime, monkeypatch):
    gate = asyncio.Event()
    _blocking_run(monkeypatch, gate)
    runtime.start(1, _actor())
    monkeypatch.setattr(runtime_module, "DRAIN_SECONDS", 0.05)

    drained = asyncio.create_task(runtime.quiesce_for_restore())
    await asyncio.sleep(0)
    with pytest.raises(ConnectorRefusal) as refused:
        runtime.start(2, _actor("b" * 8))
    assert refused.value.code == "storage_restoring" and refused.value.status == 503

    assert await drained is False  # the coordinator is still running
    gate.set()
    await asyncio.sleep(0)
    assert await runtime.quiesce_for_restore() is True


async def test_a_running_operation_reuses_its_coordinator_and_keys_are_bounded(runtime, monkeypatch):
    gate = asyncio.Event()
    _blocking_run(monkeypatch, gate)

    first = runtime.start(1, _actor())
    assert runtime.start(1, _actor()) is first
    runtime.start(2, _actor())
    with pytest.raises(ConnectorRefusal) as per_key:
        runtime.start(3, _actor())
    assert per_key.value.status == 429 and per_key.value.code == "capacity_exceeded"
    for n in range(4, 10):
        runtime.start(n, _actor(f"key-{n}"))
    with pytest.raises(ConnectorRefusal) as per_process:
        runtime.start(10, _actor("another"))
    assert per_process.value.status == 429

    gate.set()
    assert await runtime._drain(1)


async def test_an_upload_slot_outlives_a_request_that_gave_up(runtime):
    release = asyncio.Event()
    finished = []

    async def work():
        await release.wait()
        finished.append(True)
        return "stored"

    first = asyncio.create_task(runtime.run_upload(work))
    second = asyncio.create_task(runtime.run_upload(work))
    await asyncio.sleep(0)
    first.cancel()  # the client disconnected
    await asyncio.sleep(0)
    with pytest.raises(ConnectorRefusal) as refused:
        await runtime.run_upload(work)
    assert refused.value.status == 429

    release.set()
    assert await second == "stored"
    await asyncio.sleep(0)
    assert finished == [True, True]
    assert not runtime.uploads_full()


async def test_a_fence_left_by_a_crash_rotates_the_epoch_before_admission(runtime, sessions):
    async with sessions() as db:
        _server, old_epoch = await submissions.server_identity(db)
    runtime.write_restore_fence()
    assert not runtime.admitting

    restarted = ConnectorRuntime()
    await restarted.startup()

    async with sessions() as db:
        _server, new_epoch = await submissions.server_identity(db)
    assert new_epoch != old_epoch
    assert restarted.admitting
    assert not runtime_module.fence_path().exists()


async def test_a_fence_whose_rotation_fails_keeps_admission_closed(runtime, sessions, monkeypatch):
    runtime.write_restore_fence()

    async def broken(db):
        raise OSError("database unavailable")

    monkeypatch.setattr(submissions, "rotate_storage_epoch", broken)
    restarted = ConnectorRuntime()
    await restarted.startup()

    assert not restarted.admitting
    assert runtime_module.fence_path().exists()
    with pytest.raises(ConnectorRefusal) as refused:
        restarted.ensure_admitting()
    assert refused.value.code == "storage_restoring"


async def test_a_restore_that_replaced_nothing_keeps_the_epoch(runtime, sessions):
    async with sessions() as db:
        _server, epoch = await submissions.server_identity(db)
    runtime.write_restore_fence()
    runtime.clear_restore_fence()

    restarted = ConnectorRuntime()
    await restarted.startup()

    async with sessions() as db:
        assert (await submissions.server_identity(db))[1] == epoch
    assert restarted.admitting


async def test_a_restore_refuses_while_connector_work_runs(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from fastapi import HTTPException

    from backend.app.api.routes import settings as route
    from backend.app.services.backup_files import FileRestore
    from backend.tests.unit.test_backup_route_safety import upload

    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setenv("DATA_DIR", str(live))
    monkeypatch.setattr(route, "_quiesce_part_render", AsyncMock())
    monkeypatch.setattr(route, "_quiesce_library_previews", AsyncMock())
    monkeypatch.setattr("backend.app.mcp_server.runtime.mcp_runtime.quiesce_for_restore", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "backend.app.services.connector_runtime.connector_runtime.quiesce_for_restore", AsyncMock(return_value=False)
    )
    obico = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.app.services.obico_detection.obico_detection_service.quiesce", obico)
    prepare = Mock()
    monkeypatch.setattr(FileRestore, "prepare", prepare)
    db = AsyncMock()

    with pytest.raises(HTTPException) as caught:
        await route.restore_backup(file=upload(tmp_path), db=db, _=None)

    assert caught.value.status_code == 409
    obico.assert_not_awaited()
    prepare.assert_not_called()
    db.close.assert_not_awaited()
