"""Dead SMB calls cannot freeze the loop, fill its executor or publish late."""

import asyncio
from threading import Event

import pytest

from backend.app.services import source_io
from backend.app.services.filament_requirements import PrintRequirementsCache, SourceIdentity


@pytest.mark.asyncio
async def test_stalled_probe_is_bounded_reused_and_does_not_block_healthy_work(monkeypatch):
    monkeypatch.setattr(source_io, "SOURCE_IO_TIMEOUT", 0.2)
    release = Event()
    calls = []

    def stalled():
        calls.append(1)
        release.wait(10)
        return "late result"

    try:
        task = asyncio.create_task(source_io.source_probe(("stall",), stalled))
        await asyncio.sleep(0.01)
        assert not task.done()
        assert await asyncio.to_thread(lambda: "shared executor alive") == "shared executor alive"
        with pytest.raises(source_io.SourceUnavailable, match="source_timeout"):
            await task
        for _ in range(20):
            with pytest.raises(source_io.SourceUnavailable, match="source_timeout"):
                await source_io.source_probe(("stall",), stalled)
        assert calls == [1]
        monkeypatch.setattr(source_io, "SOURCE_IO_TIMEOUT", 5)
        assert await source_io.source_probe(("healthy",), lambda: 42) == 42
    finally:
        release.set()
        while ("stall",) in source_io._running:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_worker_capacity_is_waiting_not_a_false_file_failure(monkeypatch):
    monkeypatch.setattr(source_io, "_MAX_PROBES", 1)
    monkeypatch.setattr(source_io, "SOURCE_IO_TIMEOUT", 0.2)
    release = Event()
    first = asyncio.create_task(source_io.source_probe(("capacity",), release.wait, 10))
    try:
        await asyncio.sleep(0.01)
        with pytest.raises(source_io.SourceUnavailable, match="source_check_busy"):
            await source_io.source_probe(("another",), lambda: 1)
        with pytest.raises(source_io.SourceUnavailable, match="source_timeout"):
            await first
        assert len(source_io._running) == 1
    finally:
        release.set()
        while ("capacity",) in source_io._running:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_cancellation_does_not_release_the_live_os_call(monkeypatch):
    release = Event()
    task = asyncio.create_task(source_io.source_probe(("cancel",), release.wait, 10))
    try:
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ("cancel",) in source_io._running
    finally:
        release.set()
        while ("cancel",) in source_io._running:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_batch_of_missing_sources_is_probed_once_per_tick(tmp_path, monkeypatch):
    path = tmp_path / "missing.3mf"
    calls = []
    original = SourceIdentity.of

    # The real of is keyword-aware (a captured snapshot passes its sha256);
    # a stub that is not turns the probe under test into a TypeError.
    def stat(file, *, sha256=None):
        calls.append(file)
        return original(file, sha256=sha256)

    monkeypatch.setattr(SourceIdentity, "of", stat)
    cache = PrintRequirementsCache()
    for _ in range(50):
        assert (await cache.read(path, 1)).reason == "source_unreadable"
    assert calls == [path]
    assert (await PrintRequirementsCache().read(path, 1)).reason == "source_unreadable"
    assert len(calls) == 2
