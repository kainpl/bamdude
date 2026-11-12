"""Real log tools/call plus restore barriers for database and file ownership."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest

from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.mcp_server.runtime import McpRuntime
from backend.app.models.notification import NotificationProvider
from backend.app.models.oidc_provider import OIDCProvider
from backend.app.models.settings import Settings
from backend.app.services import log_reader
from backend.app.services.farm_diagnostics import logs
from backend.app.services.queue_sources import current_epoch
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import data, error, rpc
from backend.tests.unit.test_diagnostic_log_reader import inputs, line


async def log_key(client, **kw):
    created = await key(client, **kw)
    response = await client.patch(f"/api/v1/api-keys/{created['id']}", json={"can_read_mcp_logs": True})
    assert response.status_code == 200, response.text
    return created


@pytest.mark.parametrize("modern", [False, True])
async def test_logs_http_authority_redaction_and_collector_failure(
    async_client, runtime, tmp_path, monkeypatch, modern
):  # noqa: F811
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    (tmp_path / "bamdude.log").write_text(line("Authorization: Bearer CANARY_HTTP"), encoding="utf-8")
    created, scoped, disabled = (
        await log_key(async_client),
        await log_key(async_client, scope=[]),
        await key(async_client),
    )
    args = inputs().model_dump(mode="json", by_alias=True, exclude_none=True)
    async with live_app() as url, httpx.AsyncClient() as http:
        for denied in (scoped, disabled):
            assert error(await rpc(http, url, denied["key"], "bamdude_read_logs", args)) == "forbidden"
        result = data(await rpc(http, url, created["key"], "bamdude_read_logs", args, modern=modern))
        assert len(result["data"]["entries"]) == 1
        assert "CANARY" not in json.dumps(result)
        assert (
            error(await rpc(http, url, created["key"], "bamdude_read_logs", {**args, "path": "../secret"}))
            == "invalid_arguments"
        )

        async def fail(db):
            raise RuntimeError("CANARY collector secret")

        monkeypatch.setattr(log_reader, "collect_diagnostic_sensitive_strings", fail)
        blocked = Mock(side_effect=AssertionError("raw file fallback"))
        monkeypatch.setattr(logs, "read_tail", blocked)
        failed = await rpc(http, url, created["key"], "bamdude_read_logs", args)
        assert error(failed) == "source_unavailable" and "CANARY" not in json.dumps(failed)
        blocked.assert_not_called()
        await async_client.patch(f"/api/v1/api-keys/{created['id']}", json={"can_read_mcp_logs": False})
        assert error(await rpc(http, url, created["key"], "bamdude_read_logs", args)) == "forbidden"


@pytest.mark.parametrize(
    "setting",
    [
        "telegram_bot_token",
        "smtp_username",
        "smtp_password",
        "ha_token",
        "mqtt_username",
        "mqtt_password",
        "tasmota_password",
        "spoolman_api_key",
        "spoolman_token",
        "oidc_client_secret",
        "ldap_bind_password",
    ],
)
async def test_exact_setting_secret_canary(db_session, setting):
    secret = "CANARY_EXACT_" + setting
    db_session.add(Settings(key=setting, value=secret))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert secret in values
    assert "CANARY" not in log_reader.sanitize_log_content(secret, values)


@pytest.mark.parametrize("field", sorted(log_reader.PROVIDER_SECRET_FIELDS))
async def test_exact_provider_secret_canary(db_session, field):
    secret = "CANARY_PROVIDER_" + field
    db_session.add(NotificationProvider(name="test", provider_type="telegram", config=json.dumps({field: secret})))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert secret in values
    assert "CANARY" not in log_reader.sanitize_log_content(secret, values)


async def test_exact_plug_oidc_and_ha_override_canaries(db_session, smart_plug_factory, monkeypatch):
    from backend.app.core import encryption

    await smart_plug_factory(username="CANARY_PLUG_USER", password="CANARY_PLUG_PASS")
    db_session.add(
        OIDCProvider(
            name="synthetic",
            issuer_url="https://example.invalid",
            client_id="client",
            _client_secret_enc="fernet:synthetic",
        )
    )
    await db_session.commit()
    monkeypatch.setattr(encryption, "mfa_decrypt", lambda value: "CANARY_OIDC_DECRYPTED")
    monkeypatch.setenv("HA_TOKEN", "CANARY_HA_ENV")
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    for secret in ("CANARY_PLUG_USER", "CANARY_PLUG_PASS", "CANARY_OIDC_DECRYPTED", "CANARY_HA_ENV"):
        assert secret in values


async def test_file_phase_does_not_block_restore_and_old_result_is_not_published(async_client, runtime, monkeypatch):  # noqa: F811
    from backend.app.core import database
    from backend.app.services import queue_sources

    active = 0
    factory = database.async_session

    class Session:
        async def __aenter__(self):
            nonlocal active
            self.inner = factory()
            value = await self.inner.__aenter__()
            active += 1
            return value

        async def __aexit__(self, *args):
            nonlocal active
            try:
                return await self.inner.__aexit__(*args)
            finally:
                active -= 1

    monkeypatch.setattr(database, "async_session", Session)
    entered, release = threading.Event(), threading.Event()

    def slow_file(*args):
        entered.set()
        assert release.wait(15)
        return [], None, 0, 0

    monkeypatch.setattr(logs, "read_tail", slow_file)
    access = DiagnosticAccess(
        1,
        1,
        frozenset(),
        True,
        None,
        True,
        current_epoch(),
        key_identity="00000000-0000-0000-0000-000000000001",
        owner_identity="00000000-0000-0000-0000-000000000002",
    )
    task = asyncio.create_task(logs.read_logs(access, inputs(), "barrier", runtime))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert active == 0
        assert await runtime.quiesce_for_restore()
        monkeypatch.setattr(queue_sources, "current_epoch", lambda: access.epoch + 1)
        monkeypatch.setattr(logs, "current_epoch", lambda: access.epoch + 1)
        # A different runtime still shares the old process-owned file slot.
        new = McpRuntime()
        current = DiagnosticAccess(
            1,
            1,
            frozenset(),
            True,
            None,
            True,
            access.epoch + 1,
            key_identity="00000000-0000-0000-0000-000000000001",
            owner_identity="00000000-0000-0000-0000-000000000002",
        )
        with pytest.raises(Exception, match="busy"):
            await logs.read_logs(current, inputs(), "new", new)
        release.set()
        with pytest.raises(Exception, match="state_changed"):
            await task
    finally:
        release.set()
        await logs.file_reader.drain()
        if not task.done():
            await task


async def test_cancelled_database_phase_remains_owned_until_restore_drain(async_client, monkeypatch):
    owner = McpRuntime()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_db():
        entered.set()
        await release.wait()
        return "done"

    requester = asyncio.create_task(owner.database_read(blocked_db()))
    await entered.wait()
    requester.cancel()
    with pytest.raises(asyncio.CancelledError):
        await requester
    restore = asyncio.create_task(owner.quiesce_for_restore())
    try:
        await asyncio.sleep(0)
        assert not restore.done() and owner._db_tasks
        release.set()
        assert await restore
        assert not owner._db_tasks
    finally:
        release.set()
        await restore
