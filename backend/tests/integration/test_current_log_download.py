"""Current-log downloads stay finite without interrupting logging or rotation."""

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from backend.app.api.routes import support


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_list_current_and_archives_separately(async_client, log_dir):
    (log_dir / "bamdude.log").write_bytes(b"current\n")
    for name in ("bamdude-2026-09-11.log", "bamdude-2026-09-12.log", "unrelated.log"):
        (log_dir / name).write_bytes(b"archive\n")

    response = await async_client.get("/api/v1/support/log-archives")
    assert response.status_code == 200
    payload = response.json()
    assert payload["current"]["filename"] == "bamdude.log"
    assert payload["current"]["size_bytes"] == len(b"current\n")
    assert payload["current"]["mtime"]
    assert [entry["filename"] for entry in payload["archives"]] == [
        "bamdude-2026-09-12.log",
        "bamdude-2026-09-11.log",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("lines", [0, 600_000], ids=["empty", "larger-than-bundle-limit"])
async def test_download_full_snapshot_without_debug(async_client, log_dir, monkeypatch, lines):
    """No support-bundle tail cap, and copying never runs on the event loop."""
    content = ("Поточний лог\n" * lines).encode()
    (log_dir / "bamdude.log").write_bytes(content)
    loop_thread = threading.get_ident()
    real_fstat = os.fstat
    copy_threads = []

    def tracked_fstat(fd):
        copy_threads.append(threading.get_ident())
        return real_fstat(fd)

    monkeypatch.setattr(support.os, "fstat", tracked_fstat)
    monkeypatch.setattr(support, "_get_debug_setting", AsyncMock(side_effect=AssertionError("Debug is not required")))
    response = await async_client.get("/api/v1/support/logs/download")
    assert response.status_code == 200
    assert response.content == content
    assert int(response.headers["content-length"]) == len(content)
    assert 'filename="bamdude.log"' in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    assert copy_threads and loop_thread not in copy_threads


@pytest.mark.asyncio
async def test_missing_current_and_protected_download(async_client, log_dir):
    listing = await async_client.get("/api/v1/support/log-archives")
    assert listing.json() == {"archives": [], "current": None}
    missing = await async_client.get("/api/v1/support/logs/download")
    assert missing.status_code == 404
    async_client.headers.pop("Authorization")
    denied = await async_client.get("/api/v1/support/logs/download")
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_archive_actions_still_exclude_current_file(async_client, log_dir):
    current = log_dir / "bamdude.log"
    current.write_bytes(b"current\n")
    archive = log_dir / "bamdude-2026-09-12.log"
    archive.write_bytes(b"archive\n")
    response = await async_client.get(f"/api/v1/support/log-archives/{archive.name}/download")
    assert response.status_code == 200
    assert response.content == b"archive\n"
    response = await async_client.delete("/api/v1/support/log-archives/bamdude.log")
    assert response.status_code == 400
    assert current.read_bytes() == b"current\n"


@pytest.mark.asyncio
async def test_appends_excluded_and_live_handle_released_before_transfer(log_dir, monkeypatch):
    current = log_dir / "bamdude.log"
    content = b"before\n" * 20_000
    current.write_bytes(content)
    real_fstat = os.fstat

    def append_after_stat(fd):
        result = real_fstat(fd)
        with current.open("ab") as writer:
            writer.write(b"after snapshot started\n")
        return result

    monkeypatch.setattr(support.os, "fstat", append_after_stat)
    response = support.download_current_log()
    # On Windows this fails if the download still holds the live file open.
    current.rename(log_dir / "bamdude-2026-09-13.log")
    current.write_bytes(b"new day's log\n")
    messages = []

    async def send(message):
        messages.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send)
    assert b"".join(m.get("body", b"") for m in messages) == content
    assert response.snapshot.closed
    assert current.read_bytes() == b"new day's log\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("Client disconnected"), asyncio.CancelledError()])
async def test_snapshot_closed_on_interrupted_transfer(log_dir, failure):
    (log_dir / "bamdude.log").write_bytes(b"log\n")
    response = support.download_current_log()
    with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            AsyncMock(),
            AsyncMock(side_effect=failure),
        )
    assert response.snapshot.closed


@pytest.mark.asyncio
async def test_snapshot_closed_after_disconnect_during_stream(log_dir):
    """Exercise the ASGI disconnect listener used by older HTTP servers."""
    (log_dir / "bamdude.log").write_bytes(b"x" * 200_000)
    response = support.download_current_log()
    body_started = asyncio.Event()

    async def receive():
        await body_started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            body_started.set()
            await asyncio.sleep(0)

    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    assert body_started.is_set()
    assert response.snapshot.closed


def test_snapshot_removed_when_copy_fails(log_dir, monkeypatch):
    current = log_dir / "bamdude.log"
    current.write_bytes(b"before clearing\n")
    real_fstat = os.fstat
    real_temporary_file = support.tempfile.TemporaryFile
    snapshots = []

    def track_snapshot(*args, **kwargs):
        snapshot = real_temporary_file(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    def truncate_after_stat(fd):
        result = real_fstat(fd)
        current.write_bytes(b"")
        return result

    monkeypatch.setattr(support.tempfile, "TemporaryFile", track_snapshot)
    monkeypatch.setattr(support.os, "fstat", truncate_after_stat)
    with pytest.raises(HTTPException) as error:
        support.download_current_log()
    assert error.value.status_code == 409
    assert snapshots[0].closed


def test_current_log_cannot_escape_log_directory(log_dir, monkeypatch):
    real_resolve = Path.resolve

    def outside_log_dir(path, *args, **kwargs):
        if path.name == "bamdude.log":
            return log_dir.parent / "private.log"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", outside_log_dir)
    with pytest.raises(HTTPException) as error:
        support.download_current_log()
    assert error.value.status_code == 403
