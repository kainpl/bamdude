"""Historical support doors use retained secret collection before publication."""

import asyncio
import io
import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.routes import support
from backend.app.models.settings import Settings
from backend.app.services import log_reader


def entry(text):
    return "2026-11-10 12:00:00,000 ERROR [test.component] " + text + "\n"


@pytest.mark.parametrize("door", ["logs", "logs/download", "log-archives/bamdude-2026-11-09.log/download"])
async def test_historical_credentials_redacted_and_unsearchable(async_client, db_session, tmp_path, monkeypatch, door):
    secret = "opaque-мій-приватний-ключ/part"
    db_session.add(Settings(key="future_password", value=secret))
    await db_session.commit()
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    content = entry("upstream " + secret) + entry(quote(secret, safe="")) + entry("useful diagnostic OID 1.3.6.1.4.1")
    for name in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / name).write_text(content, encoding="utf-8")
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 200, response.text
    assert secret not in response.text and quote(secret, safe="") not in response.text
    assert "useful diagnostic" in response.text
    assert "1.3.6.1.4.1" in response.text
    searched = await async_client.get("/api/v1/support/logs", params={"search": secret})
    assert searched.json()["entries"] == []


async def test_configured_capability_and_custom_refused_header_collected(
    db_session, smart_plug_factory, printer_factory
):
    url = "https://device.example/control/opaque-private-capability"
    header = "opaque-header-only-secret"
    camera = "https://camera.example/private/frame-capability"
    await smart_plug_factory(plug_type="http", rest_on_url=url, rest_headers=json.dumps({"X-Custom": header}))
    await printer_factory(external_camera_url=camera)
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    for secret in (url, header, camera):
        assert secret in values
        for spelling in (secret, quote(secret, safe=""), quote(quote(secret, safe=""), safe="")):
            assert spelling not in log_reader.sanitize_log_content("peer replied " + spelling, values)


@pytest.mark.parametrize("door", ["logs", "logs/download", "log-archives/bamdude-2026-11-09.log/download"])
async def test_collector_failure_never_falls_back_to_raw(async_client, tmp_path, monkeypatch, door):
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    for name in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / name).write_text(entry("OPAQUE_CANARY_NOT_IN_SHAPE"), encoding="utf-8")
    monkeypatch.setattr(support, "collect_diagnostic_sensitive_strings", AsyncMock(side_effect=RuntimeError("failed")))
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 503
    assert "CANARY" not in response.text


async def test_bundle_and_bug_report_log_helper_use_same_collector(
    async_client, db_session, test_engine, tmp_path, monkeypatch
):
    secret = "opaque-shared-bundle-capability"
    db_session.add_all([Settings(key="future_password", value=secret), Settings(key="debug_logging", value="true")])
    await db_session.commit()
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    monkeypatch.setattr(
        support, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )
    (tmp_path / "bamdude.log").write_text(entry(secret), encoding="utf-8")
    monkeypatch.setattr(support, "_get_debug_setting", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(support, "_print_state_access", AsyncMock(return_value=(False, None)))
    roster = SimpleNamespace(printers=[], index_of={}, states={})
    monkeypatch.setattr(support, "_build_support_info", AsyncMock(return_value=({"diagnostic": secret}, roster)))
    recent = await support._get_recent_sanitized_logs()
    assert secret not in recent and "[REDACTED]" in recent
    response = await async_client.get("/api/v1/support/bundle")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert secret not in bundle.read("bamdude.log").decode()
        assert json.loads(bundle.read("support-info.json"))["diagnostic"] == "[REDACTED]"


async def test_tail_limits_never_drop_the_sql_redaction_opener(tmp_path, monkeypatch):
    raw = entry("[parameters: (") + "'CANARY_SQL_TRUNC',\n)]\n(Background on this error at: https://sqlalche.me/e/20)\n"
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    monkeypatch.setattr(support, "_log_sensitive_strings", AsyncMock(return_value={}))
    (tmp_path / "bamdude.log").write_text(raw, encoding="utf-8")
    assert "CANARY" not in support._get_log_content(max_bytes=len(raw.encode()) - 45).decode()
    assert "CANARY" not in await support._get_recent_sanitized_logs(max_lines=3)
    # A complete later parent survives; only the cut logical record is omitted.
    (tmp_path / "bamdude.log").write_text(raw + entry("useful later record"), encoding="utf-8")
    tail = support._get_log_content(max_bytes=len(raw.encode()) - 45).decode()
    assert "CANARY" not in tail and "useful later record" in tail


@pytest.mark.parametrize("door", ["logs", "logs/download", "log-archives/bamdude-2026-11-09.log/download"])
async def test_multiline_credentials_in_historical_http_doors(async_client, tmp_path, monkeypatch, door):
    raw = entry("password='first\nCANARY_MULTILINE_SECRET'")
    raw += entry("Authorization: Bearer first\n CANARY_FOLDED_HEADER")
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    for name in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / name).write_text(raw, encoding="utf-8")
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 200
    assert "CANARY" not in response.text


@pytest.mark.parametrize("bad", ["[parameters: (CANARY_SQL,)]", 'token="CANARY_QUOTE'])
@pytest.mark.parametrize("door", ["logs", "logs/download", "log-archives/bamdude-2026-11-09.log/download"])
async def test_unterminated_value_preserves_newer_entries(async_client, tmp_path, monkeypatch, door, bad):
    raw = entry(bad) + entry("NEWER useful first") + entry('NEWER status "idle"') + entry("NEWER useful last")
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    for name in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / name).write_text(raw, encoding="utf-8")
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 200
    assert "CANARY" not in response.text
    assert "NEWER useful first" in response.text and "NEWER useful last" in response.text
    monkeypatch.setattr(support, "_log_sensitive_strings", AsyncMock(return_value={}))
    assert "NEWER useful last" in await support._get_recent_sanitized_logs()
    bundle_tail = support._get_log_content().decode()
    assert "CANARY" not in bundle_tail and "NEWER useful last" in bundle_tail


@pytest.mark.parametrize("door", ["logs/download", "log-archives/bamdude-2026-11-09.log/download"])
async def test_unparented_multiline_prefix_is_not_published(async_client, tmp_path, monkeypatch, door):
    raw = "password='first\nCANARY_UNPARENTED_SECRET'\n" + entry("newer useful entry")
    monkeypatch.setattr(support.settings, "log_dir", tmp_path)
    for name in ("bamdude.log", "bamdude-2026-11-09.log"):
        (tmp_path / name).write_text(raw, encoding="utf-8")
    response = await async_client.get("/api/v1/support/" + door)
    assert response.status_code == 200
    assert "CANARY" not in response.text and "newer useful entry" in response.text


async def test_concurrent_viewer_poll_gets_busy_without_another_file_job(async_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_reader(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        return [], 0

    monkeypatch.setattr(support, "_read_log_entries", slow_reader)
    first = asyncio.create_task(async_client.get("/api/v1/support/logs"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = await async_client.get("/api/v1/support/logs")
        assert second.status_code == 503 and second.headers["retry-after"] == "2"
        assert len(calls) == 1
    finally:
        release.set()
        assert (await first).status_code == 200
