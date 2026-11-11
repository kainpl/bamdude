"""IW1: actual disk I/O remains owned after cancellation, with restore admission/drain and no DB/spool lock."""

import asyncio
import threading

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
