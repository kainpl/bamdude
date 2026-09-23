"""Bounded artifacts and cancellation retain the real I/O owner."""

import asyncio
import threading
import time
import zipfile

import pytest

from backend.app.services.preview_artifacts import disk, snapshot, validate
from backend.app.services.preview_protocol import PreviewError


def test_snapshot_rejects_over_budget_before_copy(tmp_path, monkeypatch):
    import backend.app.services.preview_artifacts as module

    monkeypatch.setattr(module, "OBJECT_BYTES", 10)
    target = tmp_path / "snapshot"
    with pytest.raises(PreviewError, match="resource_limit"):
        snapshot(b"x" * 11, target, time.monotonic_ns() + 1_000_000_000)
    assert not target.exists()


def test_zip_total_budget_and_duplicates(tmp_path, monkeypatch):
    import backend.app.services.preview_artifacts as module

    archive = tmp_path / "large.3mf"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("geometry", b"x" * 1000)
    monkeypatch.setattr(module, "ZIP_BYTES", 500)
    with pytest.raises(PreviewError, match="resource_limit"):
        validate(archive, "3mf", time.monotonic_ns() + 1_000_000_000)


@pytest.mark.asyncio
async def test_canceled_thread_await_still_owns_its_io():
    started, finish = threading.Event(), threading.Event()

    def blocked():
        started.set()
        finish.wait(5)

    task = asyncio.create_task(disk(blocked))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
