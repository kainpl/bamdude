"""Canaries and structural bounds for the MCP-only file reader."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services import log_reader
from backend.app.services.farm_diagnostics import logs
from backend.app.services.farm_diagnostics.access import DiagnosticError
from backend.app.services.farm_diagnostics.contracts import LogsInput


def inputs(**kw):
    now = datetime.now(timezone.utc)
    return LogsInput.model_validate({"from": (now - timedelta(hours=1)).isoformat(), "to": now.isoformat(), **kw})


def line(message):
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3] + " INFO [test.component] " + message + "\n"


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer CANARY_BEARER",
        "X-API-Key: CANARY_HEADER",
        "Cookie: sid=CANARY_COOKIE",
        "Set-Cookie: sid=CANARY_SETCOOKIE",
        "bd_CANARY_API",
        "bb_CANARY_API",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.CANARY_SIGNATURE",
        "123456789:CANARY_TELEGRAM_abcdefghijklmnopqrst",
        "https://discord.com/api/webhooks/123/CANARY_WEBHOOK",
        "https://hooks.slack.com/services/CANARY_WEBHOOK/T/B",
        'password="CANARY_PASSWORD with spaces"',
        "passwd=CANARY_PASSWD",
        "token: 'CANARY_TOKEN'",
        '"client_secret": "CANARY_CLIENT"',
        "api_key=CANARY_KEY",
        "access_code=CANARY_CODE",
        "secret=CANARY_SECRET",
        "gAAAAACANARY_FERNET_abcdefgh==",
        r"C:\Users\CANARY_HOME\file.txt",
        "/home/CANARY_HOME/file.txt",
        "/Users/CANARY_HOME/file.txt",
        "DETAIL: Key (password)=(CANARY_DETAIL) already exists.",
        "DETAIL: Key (password)=(\nCANARY_MULTILINE\n) already exists.",
        '"X-API-Key": "CANARY_JSON_HEADER"',
        "ftps://name:CANARY_URL@pass@host/path",
    ],
)
def test_secret_shape_canary(secret):
    assert "CANARY" not in log_reader.sanitize_log_content(secret)


def test_hash_and_unicode_survive():
    value = "SHA256 " + "a7c4" * 16 + " діагностика"
    assert log_reader.sanitize_log_content(value) == value


def test_tail_redacts_before_search_and_drops_oversized_or_partial_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    path = tmp_path / "bamdude.log"
    path.write_text(
        line("ordinary") + line("CANARY_EXACT") + line("x" * 5000) + line("Bearer CANARY_PARTIAL").rstrip(),
        encoding="utf-8",
    )
    entries, cursor, dropped, count = logs.read_tail(inputs(), {"CANARY_EXACT": "[REDACTED]"}, None)
    assert len(entries) == 2 and dropped == 2 and cursor is None
    assert "CANARY" not in str(entries)
    assert count <= logs.RAW_BUDGET
    assert logs.read_tail(inputs(search="CANARY_EXACT"), {"CANARY_EXACT": "[REDACTED]"}, None)[0] == []


def test_raw_budget_applies_without_matches_and_rotation_is_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    path = tmp_path / "bamdude.log"
    path.write_text(line("a" * 1000) * 2300, encoding="utf-8")
    entries, position, _, count = logs.read_tail(inputs(search="absent"), {}, None)
    assert not entries and position and count <= logs.RAW_BUDGET
    old = tmp_path / "old.log"
    path.rename(old)
    path.write_text(line("replacement"), encoding="utf-8")
    with pytest.raises(DiagnosticError, match="log_changed"):
        logs.read_tail(inputs(), {}, position)


async def test_timeout_keeps_file_slot_until_real_thread_exit():
    owner = logs.FileReader()
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(15)
        return "complete"

    task = asyncio.create_task(owner.read(blocked))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await task
        with pytest.raises(DiagnosticError, match="busy"):
            await owner.read(lambda: "must not run")
        drain = asyncio.create_task(owner.drain())
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        await drain
        assert await owner.read(lambda: "next") == "next"
    finally:
        release.set()
        await owner.drain()


def test_multiline_pages_keep_entry_boundaries_and_new_appends_outside_upper(tmp_path, monkeypatch):
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    path = tmp_path / "bamdude.log"
    path.write_text(
        line("older") + line("trace") + "Traceback:\n  token=CANARY_TRACE\n" + line("newer"), encoding="utf-8"
    )
    args = inputs(limit=1)
    page, position, _, _ = logs.read_tail(args, {}, None)
    assert page[0]["data_text"] == "newer" and position
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line("not in snapshot"))
    second, position, _, _ = logs.read_tail(args, {}, position)
    assert "Traceback" in second[0]["data_text"] and "CANARY" not in str(second)
    third, _, _, _ = logs.read_tail(args, {}, position)
    assert third[0]["data_text"] == "older"


def test_chunk_split_token_and_giant_json_are_not_returned_as_fragments(tmp_path, monkeypatch):
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    monkeypatch.setattr(logs, "RAW_BUDGET", 1536)
    (tmp_path / "bamdude.log").write_text(
        line('payload={"token":"CANARY_' + "x" * 3000 + '"}') + line("newest"), encoding="utf-8"
    )
    page, token, dropped, count = logs.read_tail(inputs(), {}, None)
    assert [entry["data_text"] for entry in page] == ["newest"]
    assert dropped and count <= 1536 and token
    older, _, _, _ = logs.read_tail(inputs(), {}, token)
    assert not older


def test_output_budget_paginates_without_losing_records(tmp_path, monkeypatch):
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    path = tmp_path / "bamdude.log"
    path.write_text("".join(line(f"entry-{i} " + "д" * 1500) for i in range(25)), encoding="utf-8")
    token, seen = None, []
    while True:
        entries, token, _, _ = logs.read_tail(inputs(limit=100), {}, token)
        seen += [item["data_text"].split()[0] for item in entries]
        if token is None:
            break
    assert seen == [f"entry-{i}" for i in reversed(range(25))]
