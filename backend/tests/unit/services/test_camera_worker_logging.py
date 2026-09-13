import asyncio
import io
import json
import logging
from types import SimpleNamespace

import pytest

from backend.app.services.camera_worker_logging import (
    PREFIX,
    WorkerLogHandler,
    decode_worker_log,
    set_camera_log_secrets,
)
from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor


def test_worker_record_redacts_credentials_before_truncation(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("sys.stderr", output)
    set_camera_log_secrets("private-code", "https://camera/frame?token=private-token")
    try:
        record = logging.LogRecord(
            "camera.transport",
            logging.WARNING,
            __file__,
            1,
            "failed %s private-code https://camera/frame?token=private-token\n" + "x" * 9000,
            ("rtsps://user:another-password@camera/live?secret=query-secret",),
            None,
        )
        WorkerLogHandler().emit(record)
    finally:
        set_camera_log_secrets()
    line = output.getvalue()
    assert "private-code" not in line
    assert "private-token" not in line
    assert "another-password" not in line
    assert "query-secret" not in line
    decoded = decode_worker_log(line.encode())
    assert decoded is not None
    assert decoded[:2] == (logging.WARNING, "camera.transport")
    assert len(decoded[2]) <= 2048
    assert "\n" not in decoded[2]


def test_exception_keeps_type_and_location_without_message_or_locals(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("sys.stderr", output)
    try:
        raise RuntimeError("opaque-secret-not-in-context")
    except RuntimeError as error:
        record = logging.LogRecord(
            "camera.transport",
            logging.ERROR,
            __file__,
            1,
            "Capture failed",
            (),
            (type(error), error, error.__traceback__),
        )
        WorkerLogHandler().emit(record)
    assert "RuntimeError" in output.getvalue()
    assert "test_camera_worker_logging.py" in output.getvalue()
    assert "opaque-secret-not-in-context" not in output.getvalue()


@pytest.mark.asyncio
async def test_stderr_relay_handles_fragmented_unicode_and_discards_oversized_raw_lines(caplog):
    reader = asyncio.StreamReader()
    supervisor = CameraWorkerSupervisor()
    supervisor.process = SimpleNamespace(stderr=reader)
    line = (
        PREFIX
        + json.dumps({"level": 30, "logger": "camera.transport", "message": "Камера failed"}, ensure_ascii=False)
        + "\n"
    ).encode()
    with caplog.at_level(logging.INFO):
        drain = asyncio.create_task(supervisor._drain_stderr())
        # Deliberately split inside UTF-8 and JSON; raw stderr never reaches logs.
        for byte in line:
            reader.feed_data(bytes([byte]))
            await asyncio.sleep(0)
        reader.feed_data(b"raw-password " + b"x" * 20000 + b"\n")
        reader.feed_data(line)
        reader.feed_eof()
        await drain
    assert caplog.text.count("Камера failed") == 2
    assert "raw-password" not in caplog.text
    assert "unstructured_lines=1" in caplog.text


@pytest.mark.asyncio
async def test_stderr_relay_rate_limits_a_noisy_worker(caplog):
    reader = asyncio.StreamReader()
    supervisor = CameraWorkerSupervisor()
    supervisor.process = SimpleNamespace(stderr=reader)
    line = (PREFIX + json.dumps({"level": 30, "logger": "camera.transport", "message": "retrying"}) + "\n").encode()
    reader.feed_data(line * 250)
    reader.feed_eof()
    with caplog.at_level(logging.INFO):
        await supervisor._drain_stderr()
    assert sum("retrying" in r.message for r in caplog.records) == 200
    assert "rate_limited_records=50" in caplog.text


@pytest.mark.parametrize("line", [b"raw secret\n", (PREFIX + "{}").encode(), (PREFIX + "[]").encode()])
def test_unstructured_or_invalid_stderr_is_not_forwarded(line):
    assert decode_worker_log(line) is None
