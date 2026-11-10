"""AA1/AA2: lazy bounded framing, cancellation ownership and entry barriers."""

import asyncio
import io
import json
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from backend.app.core.logging_filters import redact_log_credentials
from backend.app.services import log_reader


def line(message, level="INFO"):
    return f"2026-11-10 10:00:00,000 {level} [backend.app.x] {message}\n"


def test_large_reader_sanitizes_only_needed_entries_and_compiles_map_once(tmp_path, monkeypatch):
    path = tmp_path / "bamdude.log"
    raw = line("mqtt push layer=123 payload={'nozzle_temper':220.5,'bed_temper':60}").encode()
    path.write_bytes(raw * 220000)
    original = log_reader._sanitize_log_content
    sizes, compilations = [], []
    compiler = log_reader._known_value_replacer

    def compile_once(values):
        compilations.append(values)
        return compiler(values)

    def bounded(value, replace):
        sizes.append(len(value.encode()))
        return original(value, replace)

    monkeypatch.setattr(log_reader, "_sanitize_log_content", bounded)
    monkeypatch.setattr(log_reader, "_known_value_replacer", compile_once)
    entries, count = log_reader.read_log_entries(200, log_file=path)
    assert len(entries) == 200 and count == 220000
    assert len(compilations) == 1
    assert len(sizes) == 400  # message + logger, never 220000 old entries
    assert max(sizes) <= log_reader.LOG_ENTRY_BUDGET


def test_tail_parent_separators_and_cut_or_oversized_records(tmp_path):
    path = tmp_path / "bamdude.log"
    raw = "'CANARY_ORPHAN',\n)]\n" + line("first") + line("password='CANARY_BIG" + "x" * 70000)
    raw += line("third\ntraceback continuation") + line("last")
    path.write_text(raw, encoding="utf-8")
    entries, count = log_reader.read_log_entries(20, log_file=path)
    assert [entry.message.splitlines()[0] for entry in entries] == ["last", "third", "first"]
    assert entries[0].message == "last" and entries[-1].message == "first"
    assert count == len(raw.splitlines())
    tail = log_reader.read_sanitized_log_tail(path, {}, max_entries=2).decode()
    assert tail.splitlines() == (line("third\ntraceback continuation") + line("last")).splitlines()
    assert "CANARY" not in log_reader.read_sanitized_log_tail(path).decode()
    for cap in (40, 80, 140):
        assert "CANARY" not in log_reader.read_sanitized_log_tail(path, max_bytes=cap).decode()
    path.write_text(line("first") + line("second") + line("third"), encoding="utf-8")
    assert log_reader.read_sanitized_log_tail(path).decode().splitlines() == [
        line("first").strip(),
        line("second").strip(),
        line("third").strip(),
    ]


@pytest.mark.parametrize("bad", ["[parameters: (CANARY_SQL,)]", 'token="CANARY_QUOTE'])
@pytest.mark.parametrize("encoding", ["raw", "percent", "double", "json"])
def test_direct_full_string_reframes_after_decoding(bad, encoding):
    raw = line(bad, "ERROR") + line("LATER first") + line('LATER status "idle"') + line("LATER last")
    text = raw
    if encoding == "percent":
        text = quote(raw, safe="")
    elif encoding == "double":
        text = quote(quote(raw, safe=""), safe="")
    elif encoding == "json":
        text = json.dumps(raw)[1:-1]
    for sanitize in (redact_log_credentials, log_reader.sanitize_log_content):
        safe = sanitize(text)
        assert "CANARY" not in safe
        assert "LATER first" in safe and "LATER last" in safe
        assert len(safe.splitlines()) == 4


@pytest.mark.parametrize(
    "prefix", ["password='first\nCANARY_UNPARENTED_SECRET'\n", "[parameters: (\nCANARY_SQL_PREFIX\n"]
)
def test_snapshot_unparented_prefix_keeps_credential_opener(prefix):
    raw = (prefix + line("newer entry")).encode()
    target = io.BytesIO()
    log_reader.write_sanitized_log_snapshot(io.BytesIO(raw), target, len(raw), {})
    assert b"CANARY" not in target.getvalue()
    assert b"newer entry" in target.getvalue()


@pytest.mark.parametrize(
    "text",
    [
        r"status=idle\nnozzle=220",
        "ordinary%20diagnostic%0Amessage",
        r"C:\\logs\\firmware.txt",
        "status &amp; firmware 01.09.01.00",
    ],
)
def test_ordinary_encoded_diagnostics_preserved_verbatim(text):
    assert redact_log_credentials(text) == text


def test_sanitized_output_cap_and_short_value_expansion(tmp_path):
    path = tmp_path / "bamdude.log"
    path.write_bytes(line("x" * 120).encode() * 9000)
    cap = 1024 * 1024
    tail = log_reader.read_sanitized_log_tail(path, {"x": "[REDACTED]"}, max_bytes=cap)
    assert 0 < len(tail) <= cap
    huge = line("x" * 16000).encode()
    target = io.BytesIO()
    log_reader.write_sanitized_log_snapshot(io.BytesIO(huge), target, len(huge), {"x": "[REDACTED]"})
    assert b"omitted oversized sanitized" in target.getvalue()
    assert len(target.getvalue()) < 100


async def test_busy_owner_has_no_queue_and_cancel_retains_real_thread():
    owner = log_reader.LogFileWork()
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(10)
        return "done"

    task = asyncio.create_task(owner.run(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(log_reader.LogReadUnavailable, match="busy"):
            await owner.run(lambda: "must not run")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(log_reader.LogReadUnavailable, match="busy"):
            await owner.run(lambda: "still must not run")
    finally:
        release.set()
        await owner.drain()
        owner._executor.shutdown(wait=True)


async def test_cancel_between_function_return_and_future_completion_closes_snapshot():
    owner = log_reader.LogFileWork()
    owner._executor.shutdown(wait=True)
    returned, complete = threading.Event(), threading.Event()
    snapshot = io.BytesIO(b"snapshot")

    class DelayedExecutor:
        def submit(self, function):
            future = Future()

            def run():
                try:
                    result = function()  # cancelled check/finally already ran
                    returned.set()
                    assert complete.wait(10)
                    future.set_result(result)
                except BaseException as exc:
                    future.set_exception(exc)

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()
            return future

    owner._executor = DelayedExecutor()
    task = asyncio.create_task(owner.run(lambda: SimpleNamespace(snapshot=snapshot)))
    try:
        assert await asyncio.to_thread(returned.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not snapshot.closed  # future is still PENDING: outer cleanup cannot own result yet
    finally:
        complete.set()
        await owner.drain()
        await asyncio.to_thread(owner._executor.thread.join, 5)
    assert snapshot.closed
