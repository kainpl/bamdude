"""RA1/RA2: stopping recording is independent of best-effort log collection."""

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.routes import bug_report, support
from backend.app.i18n.api_errors import translate_sentence
from backend.app.services import log_reader

DEADLINE_MESSAGE = "Log processing exceeded the time limit. Use a support bundle or narrow your search."
NOTICE = "Recent logs are unavailable. You can still submit this report."


@pytest.fixture
async def recording(test_engine, monkeypatch, tmp_path):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(bug_report, "async_session", factory)
    monkeypatch.setattr(bug_report, "printer_manager", SimpleNamespace(_clients={}))
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    monkeypatch.setattr(support, "collect_diagnostic_sensitive_strings", AsyncMock(return_value={}))
    (tmp_path / "bamdude.log").write_text(
        '2026-11-10 12:00:00,000 INFO [test] token="CANARY_SECRET"\n', encoding="utf-8"
    )
    loggers = [
        logging.getLogger(name) for name in ("", "sqlalchemy.engine", "aiosqlite", "httpcore", "httpx", "paho.mqtt")
    ]
    levels = [(item, item.level) for item in loggers + logging.getLogger().handlers]
    owner = log_reader.LogFileWork()
    monkeypatch.setattr(support, "file_work", owner)
    try:
        yield factory, owner
    finally:
        await owner.drain()
        owner._executor.shutdown(wait=True)
        for item, level in levels:
            item.setLevel(level)


async def assert_level(factory, enabled):
    # Reopen the DB: checking an already-loaded ORM row would miss a lost commit.
    async with factory() as db:
        persisted, enabled_at = await support._get_debug_setting(db)
    assert persisted is enabled
    assert (enabled_at is not None) is enabled
    assert logging.getLogger().level == (logging.DEBUG if enabled else logging.INFO)
    assert all(handler.level == logging.getLogger().level for handler in logging.getLogger().handlers)


@pytest.mark.parametrize("was_debug", [False, True])
@pytest.mark.parametrize("failure", ["busy", "deadline", "secret_db", "file_io"])
async def test_stop_restores_persisted_and_runtime_level_when_tail_fails(
    async_client, recording, monkeypatch, failure, was_debug
):
    factory, owner = recording
    if was_debug:
        async with factory() as db:
            await support._set_debug_setting(db, True)
        support._apply_log_level(True)
    started = await async_client.post("/api/v1/bug-report/start-logging")
    assert started.status_code == 200 and started.json()["was_debug"] is was_debug
    await assert_level(factory, True)
    release, entered = threading.Event(), threading.Event()
    held = None

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return b"CANARY_MUST_NOT_BE_RETURNED"

    if failure == "busy":
        held = asyncio.create_task(owner.run(blocked))
        assert await asyncio.to_thread(entered.wait, 5)
    elif failure == "deadline":
        original_run = owner.run

        async def short_run(function, *args, timeout=30, **kwargs):
            return await original_run(function, *args, timeout=0.01, **kwargs)

        monkeypatch.setattr(owner, "run", short_run)
        monkeypatch.setattr(support, "read_sanitized_log_tail", blocked)
    elif failure == "secret_db":
        monkeypatch.setattr(
            support,
            "collect_diagnostic_sensitive_strings",
            AsyncMock(side_effect=OperationalError("SELECT", {}, RuntimeError("CANARY_DB_SECRET"))),
        )
    else:

        def denied(*args, **kwargs):
            raise PermissionError("CANARY_FILE_SECRET")

        monkeypatch.setattr(support, "read_sanitized_log_tail", denied)
    try:
        response = await async_client.post(f"/api/v1/bug-report/stop-logging?was_debug={str(was_debug).lower()}")
        assert response.status_code == 200, response.text
        assert response.json() == {"logs": NOTICE}
        assert "CANARY" not in response.text
        await assert_level(factory, was_debug)
    finally:
        release.set()
        if held is not None:
            await held


@pytest.mark.parametrize("was_debug", [False, True])
async def test_stop_normal_path_keeps_sanitized_logs_and_prior_debug_intent(async_client, recording, was_debug):
    factory, _ = recording
    async with factory() as db:
        await support._set_debug_setting(db, True)
    support._apply_log_level(True)
    response = await async_client.post(f"/api/v1/bug-report/stop-logging?was_debug={str(was_debug).lower()}")
    assert response.status_code == 200
    assert "[REDACTED]" in response.json()["logs"] and "CANARY" not in response.text
    await assert_level(factory, was_debug)


async def test_cancelled_tail_still_restores_the_recording_level(recording, monkeypatch):
    factory, _ = recording
    async with factory() as db:
        await support._set_debug_setting(db, True)
    support._apply_log_level(True)
    entered = asyncio.Event()

    async def waiting():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bug_report, "_get_recent_sanitized_logs", waiting)
    stopped = asyncio.create_task(bug_report.stop_logging(was_debug=False))
    await asyncio.wait_for(entered.wait(), 5)
    stopped.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopped
    await assert_level(factory, False)


async def test_failed_restoration_write_lowers_runtime_but_does_not_claim_success(recording, monkeypatch):
    factory, _ = recording
    async with factory() as db:
        await support._set_debug_setting(db, True)
    support._apply_log_level(True)
    monkeypatch.setattr(bug_report, "_get_recent_sanitized_logs", AsyncMock(return_value="safe diagnostic"))
    monkeypatch.setattr(
        bug_report,
        "_set_debug_setting",
        AsyncMock(side_effect=OperationalError("UPDATE", {}, RuntimeError("database unavailable"))),
    )
    with pytest.raises(OperationalError):
        await bug_report.stop_logging(was_debug=False)
    assert logging.getLogger().level == logging.INFO
    async with factory() as db:
        assert (await support._get_debug_setting(db))[0] is True


@pytest.mark.parametrize("mode", ["worker_checkpoint", "async_deadline"])
async def test_both_deadline_paths_give_honest_refusal_without_retry_after(mode):
    owner = log_reader.LogFileWork()
    release = threading.Event()

    def work():
        if mode == "worker_checkpoint":
            log_reader._file_context.deadline = 0
            log_reader._file_checkpoint()
        assert release.wait(5)

    try:
        with patch.object(support, "file_work", owner), pytest.raises(HTTPException) as refused:
            await support._run_log_file_work(work, timeout=0.01)
        assert refused.value.status_code == 503
        assert refused.value.detail == DEADLINE_MESSAGE
        assert not refused.value.headers or "Retry-After" not in refused.value.headers
    finally:
        release.set()
        await owner.drain()
        owner._executor.shutdown(wait=True)


@pytest.mark.parametrize(
    "door", ["logs?search=NO_SUCH_RECORD", "logs/download", "log-archives/bamdude-2026-11-09.log/download"]
)
async def test_large_file_deadline_is_not_reported_as_busy(async_client, recording, monkeypatch, tmp_path, door):
    _, owner = recording
    line = b"2026-11-10 12:00:00,000 INFO [test] regular bounded log record " + b"x" * 300 + b"\n"
    content = line * (26_000_000 // len(line) + 1)
    assert len(content) >= 26_000_000
    for name in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / name).write_bytes(content)
    original_run = owner.run

    async def short_run(function, *args, timeout=30, **kwargs):
        # Run the real 26 MB reader with a scaled deadline; no 10/30-second sleep.
        return await original_run(function, *args, timeout=0.005, **kwargs)

    monkeypatch.setattr(owner, "run", short_run)
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 503
    assert response.json()["detail"] == DEADLINE_MESSAGE
    assert "retry-after" not in response.headers


def test_deadline_refusal_has_ukrainian_copy():
    assert translate_sentence(DEADLINE_MESSAGE, "uk") != DEADLINE_MESSAGE
