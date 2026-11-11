"""A library upload writes its bytes off the event loop (P7 risk audit, 2026-11-08).

``store_library_upload`` is the one ingest of the browser's library upload and the slicer connector. It used to write
the whole body with a synchronous ``open(...).write`` on the loop: a 505 MiB file froze the whole server for about two
seconds (every request, socket push and MQTT callback), measured on a stand with the connector's cap at 512 MiB. The
test makes that write slow and counts what the loop does meanwhile.
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from pathlib import Path

import pytest

from backend.app.api.routes import library
from backend.app.core.config import settings as app_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SLOW_WRITE = 0.6


def _3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", b"<model/>")
    return buf.getvalue()


@pytest.fixture
def isolated_archive_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_settings, "archive_dir", str(tmp_path), raising=False)
    return tmp_path


async def test_the_upload_write_lets_the_event_loop_run(db_session, isolated_archive_dir, monkeypatch):
    real_open = open
    writes = []

    class SlowFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            time.sleep(SLOW_WRITE)  # a large file on a busy disk
            writes.append(len(data))
            return self.handle.write(data)

    def slow_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return SlowFile(handle) if "w" in mode else handle

    monkeypatch.setattr(library, "open", slow_open, raising=False)
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    beat = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        started = time.monotonic()
        result = await library.store_library_upload(
            db_session, filename="large.3mf", content=_3mf(), target_folder=None
        )
        elapsed = time.monotonic() - started
    finally:
        done.set()
        await beat

    assert writes, "the slow write was not the one the upload used"
    assert result.outcome == "created"
    # On the loop, the ticker could not run during the write; off it, it keeps its ~50 beats a second.
    assert elapsed >= SLOW_WRITE and ticks >= int(SLOW_WRITE / 0.02 / 2), (ticks, elapsed)
