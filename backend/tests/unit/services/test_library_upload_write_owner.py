"""IW1: actual disk I/O remains owned after cancellation, with restore admission/drain and no DB/spool lock."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.library_file_runtime import UploadWriteOwner, UploadWritesClosed

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_thread_blocks_restore_until_finished_and_new_writes_stay_closed(tmp_path, cancel):
    owner = UploadWriteOwner()
    entered, release = threading.Event(), threading.Event()
    target = tmp_path / "owned"

    def write():
        entered.set()
        release.wait(5)
        target.write_bytes(b"owned")

    task = asyncio.create_task(owner.run(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # Repeated cancellation still cannot return before the syscall ends.
        assert not await owner.quiesce(0)
        assert owner.closed and len(owner.tasks) == 1 and not task.done()
        with pytest.raises(UploadWritesClosed):
            await owner.run(lambda: pytest.fail("new thread must not start"))
        with pytest.raises(RuntimeError):
            owner.reopen()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert await owner.quiesce(1)
        assert not owner.tasks and target.read_bytes() == b"owned"
        assert owner.closed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_write_failure_releases_actual_owner_and_propagates():
    owner = UploadWriteOwner()

    def failed():
        raise OSError("disk full")

    with pytest.raises(OSError):
        await owner.run(failed)
    assert await owner.quiesce(0)
    assert not owner.tasks


async def test_restore_gate_refuses_before_preview_or_storage_work(monkeypatch):
    from backend.app.api.routes import settings
    from backend.app.services import library_file_runtime, library_preview_jobs

    class BlockedOwner:
        async def quiesce(self):
            return False

    async def must_not_run():
        pytest.fail("restore cannot reach preview/storage work before actual upload writers stop")

    monkeypatch.setattr(library_file_runtime, "upload_writes", BlockedOwner())
    monkeypatch.setattr(library_preview_jobs, "quiesce_library_previews_for_restore", must_not_run)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await settings._quiesce_library_previews()
    assert error.value.status_code == 409


@pytest.mark.parametrize("write_running", [True, False])
@pytest.mark.parametrize("runtime_present", [True, False])
async def test_lifespan_shutdown_completes_cleanly_when_upload_outlives_drain(
    tmp_path, monkeypatch, caplog, write_running, runtime_present
):
    from backend.app import main
    from backend.app.mcp_server.runtime import mcp_runtime
    from backend.app.services import farm_startup_guard, library_file_runtime
    from backend.app.services.connector_runtime import connector_runtime

    guard = farm_startup_guard.FarmStartupGuard()
    monkeypatch.setattr(main, "farm_startup_guard", guard)
    monkeypatch.setattr(farm_startup_guard, "farm_startup_guard", guard)
    monkeypatch.setattr(main.app_settings, "data_dir", tmp_path)
    monkeypatch.setattr(main.process_gc, "prepare_startup_heap", lambda: None)
    for target, methods in ((mcp_runtime, ("reload", "shutdown")), (connector_runtime, ("startup", "shutdown"))):
        for name in methods:
            monkeypatch.setattr(target, name, AsyncMock())
    state_dir = tmp_path / "runtime-state" / "farm-startup"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {"version": 1, "boot_id": "00000000-0000-4000-8000-000000000001", "lifecycle": "clean", "hold": None}
        ),
        encoding="utf-8",
    )
    owner = UploadWriteOwner()
    monkeypatch.setattr(library_file_runtime, "upload_writes", owner)
    quiesce = owner.quiesce

    async def short_drain():
        return await quiesce(0.01)

    monkeypatch.setattr(owner, "quiesce", short_drain)
    worker = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(library_file_runtime, "runtime", worker if runtime_present else None)
    later_steps = []

    @asynccontextmanager
    async def application_services(app):
        yield
        await library_file_runtime.stop_library_file_runtime()
        later_steps.append("remaining shutdown, including database stop")

    monkeypatch.setattr(main, "_application_lifespan", application_services)
    entered, release = threading.Event(), threading.Event()
    target = tmp_path / "upload-secret-path"

    def write():
        entered.set()
        assert release.wait(10)
        target.write_bytes(b"complete")

    task = None
    try:
        with caplog.at_level("WARNING", logger=library_file_runtime.__name__):
            async with main.lifespan(main.app):
                assert guard.state["hold"] is None
                if write_running:
                    task = asyncio.create_task(owner.run(write))
                    assert await asyncio.to_thread(entered.wait, 2)
        assert later_steps and owner.closed and library_file_runtime.runtime is None
        assert worker.stop.await_count == int(runtime_present)
        assert json.loads(state_path.read_text(encoding="utf-8"))["lifecycle"] == "clean"
        if task is not None:
            assert not task.done() and len(owner.tasks) == 1
        warnings = [r for r in caplog.records if r.name == library_file_runtime.__name__]
        assert [r.getMessage() for r in warnings] == (
            ["Library upload writes were still running at shutdown"] if write_running else []
        )
        assert all(r.exc_info is None for r in warnings)
        guard.start(tmp_path)
        assert guard.state["hold"] is None
    finally:
        release.set()
        if task is not None:
            await task
        guard.finish(clean=False)
    assert not owner.tasks and owner.closed
    if write_running:
        assert target.read_bytes() == b"complete"
