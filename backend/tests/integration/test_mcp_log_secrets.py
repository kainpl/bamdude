"""Persisted opaque credentials must never reach MCP log text or search."""

import json
from unittest.mock import Mock

import httpx
import pytest

from backend.app.models.cloud_link import CloudLink
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import log_reader
from backend.app.services.farm_diagnostics import logs
from backend.tests.integration.test_mcp_base_transport import live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import error, rpc
from backend.tests.integration.test_mcp_logs import log_key
from backend.tests.unit.test_diagnostic_log_reader import inputs, line

SETTINGS_CREDENTIALS = ("bambu_cloud_token", "obico_ml_token", "prometheus_token", "auth_secret_key")
USER_CREDENTIALS = (
    "cloud_token",
    "cloud_refresh_token",
    "orca_cloud_token",
    "orca_cloud_refresh_token",
    "orca_cloud_pending_verifier",
    "orca_cloud_pending_state",
)
SOURCES = [
    *("settings." + key for key in SETTINGS_CREDENTIALS),
    *("users." + key for key in USER_CREDENTIALS),
    "cloud_link",
]


@pytest.mark.parametrize("source", SOURCES)
async def test_persisted_opaque_secret_is_collected_before_log_search(db_session, tmp_path, monkeypatch, source):
    from cryptography.fernet import Fernet

    from backend.app.core import encryption

    # The PKCE state column is only 32 characters on PostgreSQL.
    secret = f"opaque-canary-{SOURCES.index(source):02}-value"
    assert log_reader.sanitize_log_content(secret) == secret  # cannot pass on shape rules alone
    if source.startswith("settings."):
        db_session.add(Settings(key=source.split(".")[1], value=secret))
    elif source.startswith("users."):
        db_session.add(User(username="opaque-canary", is_active=False, **{source.split(".")[1]: secret}))
    else:
        monkeypatch.setattr(encryption, "_fernet_instance", Fernet(Fernet.generate_key()))
        encrypted = encryption.mfa_encrypt(secret)
        assert encrypted != secret and encrypted.startswith("fernet:")
        db_session.add(CloudLink(id=1, instance_secret_encrypted=encrypted))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert secret in values
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    (tmp_path / "bamdude.log").write_text(line("upstream replied " + secret), encoding="utf-8")
    entries = logs.read_tail(inputs(), values, None)[0]
    assert len(entries) == 1 and secret not in json.dumps(entries)
    assert logs.read_tail(inputs(search=secret), values, None)[0] == []


async def test_every_sensitive_settings_key_is_collected_with_literal_case_insensitive_rule(db_session):
    from sqlalchemy import select

    from backend.app.core.sensitive_settings import SENSITIVE_SETTING_FRAGMENTS, is_sensitive_setting

    # New, mixed-case setting names exercise every fragment independently.
    expected = {
        f"future_{marker.upper()}_value": f"opaque-config-{i}-value"
        for i, marker in enumerate(sorted(SENSITIVE_SETTING_FRAGMENTS))
    }
    controls = {"preferred_slicer": "opaque-control-value", "futureXip_value": "opaque-like-underscore-control"}
    db_session.add_all([Settings(key=key, value=value) for key, value in (expected | controls).items()])
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    for key, value in (await db_session.execute(select(Settings.key, Settings.value))).all():
        if is_sensitive_setting(key) and value:
            assert value in values, key
    assert all(value in values for value in expected.values())
    assert all(not is_sensitive_setting(key) and value not in values for key, value in controls.items())


@pytest.mark.parametrize("value", ["1", "12", "true", "false", "1200", "1.25", "-2e10", "null"])
async def test_trivial_private_setting_does_not_destroy_log_headers(db_session, tmp_path, monkeypatch, value):
    db_session.add(Settings(key="future_host_config", value=value))
    await db_session.commit()
    values = await log_reader.collect_diagnostic_sensitive_strings(db_session)
    assert value not in values
    monkeypatch.setattr(logs.settings, "log_dir", tmp_path)
    (tmp_path / "bamdude.log").write_text(line("job 1200 done"), encoding="utf-8")
    entries = logs.read_tail(inputs(), values, None)[0]
    assert len(entries) == 1 and "job 1200 done" in json.dumps(entries)


async def test_short_real_credentials_are_still_redacted(db_session):
    db_session.add(Settings(key="future_password", value="1"))
    await db_session.commit()
    assert "1" in await log_reader.collect_diagnostic_sensitive_strings(db_session)


async def test_cloud_link_decryption_failure_does_not_read_or_publish_logs(
    async_client, runtime, db_session, monkeypatch
):  # noqa: F811
    from backend.app.core import encryption

    db_session.add(CloudLink(id=1, instance_secret_encrypted="fernet:unreadable"))
    await db_session.commit()

    def failed_decrypt(value):
        raise RuntimeError("opaque-decryption-error-with-secret")

    monkeypatch.setattr(encryption, "mfa_decrypt", failed_decrypt)
    blocked = Mock(side_effect=AssertionError("file read after failed decryption"))
    monkeypatch.setattr(logs, "read_tail", blocked)
    created = await log_key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        result = await rpc(
            http,
            url,
            created["key"],
            "bamdude_read_logs",
            inputs().model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    assert error(result) == "source_unavailable"
    assert "opaque-decryption-error" not in json.dumps(result)
    blocked.assert_not_called()
