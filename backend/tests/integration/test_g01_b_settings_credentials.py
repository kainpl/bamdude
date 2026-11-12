"""Unit B settings sender contracts: private reads, locked drafts and atomic edits."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app.api.routes import auth, obico, settings
from backend.app.core.credential_config import lock_credential_config
from backend.app.models.settings import Settings
from backend.app.schemas.auth import SMTPSettingsUpdate, TestSMTPRequest as SMTPProbe
from backend.app.schemas.settings import AppSettingsUpdate
from backend.tests.auth_helpers import mint_test_access

pytestmark = pytest.mark.asyncio


async def seed(db, values):
    for key, value in values.items():
        await settings.set_setting(db, key, str(value).lower() if isinstance(value, bool) else str(value))
    await db.commit()


async def resolve(db, changes, actions=None):
    await lock_credential_config(db)
    return await settings.resolve_settings_credentials(db, changes, actions)


async def test_read_variants_exclude_all_secret_values(async_client, db_session, monkeypatch):
    values = {
        key: "12345678" if key == "virtual_printer_access_code" else "b-secret-canary"
        for key in settings.SETTINGS_SECRETS
    }
    await seed(db_session, values)
    monkeypatch.setenv("HA_TOKEN", "b-env-canary")
    for method, path, payload in [
        ("GET", "/settings/", None),
        ("PUT", "/settings/", {"currency": "EUR"}),
        ("PATCH", "/settings", {"currency": "USD"}),
        ("POST", "/settings/electricity-price", {"energy_cost_per_kwh": 0.2}),
    ]:
        response = await async_client.request(method, "/api/v1" + path, json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert not settings.SETTINGS_SECRETS & body.keys()
        assert all(body[f"{key}_configured"] for key in settings.SETTINGS_SECRETS)
        assert "canary" not in response.text


@pytest.mark.parametrize("field", sorted(settings.SETTINGS_SECRETS))
async def test_settings_actions_keep_clear_replace(db_session, field):
    old, new = ("12345678", "87654321") if field == "virtual_printer_access_code" else ("old-canary", "new-canary")
    await seed(db_session, {field: old})
    assert (await resolve(db_session, {}, {field: "keep"}))[field] == old
    assert (await resolve(db_session, {}, {field: "clear"}))[field] == ""
    assert (await resolve(db_session, {field: new}, {field: "replace"}))[field] == new
    assert (await resolve(db_session, {}))[field] == old
    assert await settings.get_setting(db_session, field) == old  # Draft resolution never writes.
    for changes, action in [({field: new}, "keep"), ({field: new}, "clear"), ({}, "replace"), ({field: ""}, "replace")]:
        with pytest.raises(HTTPException) as error:
            await resolve(db_session, changes, {field: action})
        assert error.value.status_code == 422


@pytest.mark.parametrize("field", sorted(settings.SETTINGS_SECRETS))
async def test_keep_without_existing_secret_refuses(db_session, field):
    with pytest.raises(HTTPException) as error:
        await resolve(db_session, {}, {field: "keep"})
    assert error.value.status_code == 400


@pytest.mark.parametrize(
    ("secret", "stored", "changed"),
    [
        ("mqtt_password", {"mqtt_broker": "old", "mqtt_port": 8883, "mqtt_use_tls": True}, {"mqtt_broker": "new"}),
        ("mqtt_password", {"mqtt_broker": "old", "mqtt_use_tls": True}, {"mqtt_use_tls": False}),
        ("mqtt_password", {"mqtt_username": "old"}, {"mqtt_username": "new"}),
        ("mqtt_password", {"mqtt_port": 1883}, {"mqtt_port": 1884}),
        ("ha_token", {"ha_url": "http://ha.local/base"}, {"ha_url": "http://ha.local/other"}),
        ("ldap_bind_password", {"ldap_server_url": "ldap://old.local"}, {"ldap_server_url": "ldap://new.local"}),
        (
            "ldap_bind_password",
            {"ldap_server_url": "ldap://old.local", "ldap_security": "ldaps"},
            {"ldap_security": "starttls"},
        ),
        ("ldap_bind_password", {"ldap_bind_dn": "cn=old"}, {"ldap_bind_dn": "cn=new"}),
        ("obico_ml_token", {"obico_ml_url": "http://ml.local/a"}, {"obico_ml_url": "http://ml.local/b"}),
    ],
)
async def test_destination_change_cannot_retain_secret(db_session, monkeypatch, secret, stored, changed):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await seed(db_session, {**stored, secret: "old-canary"})
    with pytest.raises(HTTPException) as error:
        await resolve(db_session, changed)
    assert error.value.status_code == 400
    assert (await resolve(db_session, {**changed, secret: "replacement"}, {secret: "replace"}))[secret] == "replacement"


@pytest.mark.parametrize(
    ("secret", "stored", "changed"),
    [
        ("ha_token", {"ha_url": "http://HA.local:80/base/"}, {"ha_url": "http://ha.local/base"}),
        ("obico_ml_token", {"obico_ml_url": "http://ML.local:80/base/"}, {"obico_ml_url": "http://ml.local/base"}),
        (
            "ldap_bind_password",
            {"ldap_server_url": "ldap://LDAP.local", "ldap_security": "starttls"},
            {"ldap_server_url": "ldap://ldap.local:389"},
        ),
        (
            "ldap_bind_password",
            {"ldap_server_url": "ldaps://LDAP.local", "ldap_security": "ldaps"},
            {"ldap_server_url": "ldaps://ldap.local:636"},
        ),
    ],
)
async def test_sender_normalized_noops_keep_credentials(db_session, monkeypatch, secret, stored, changed):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await seed(db_session, {**stored, secret: "old-canary"})
    assert (await resolve(db_session, changed))[secret] == "old-canary"


@pytest.mark.parametrize(
    ("field", "enabled"),
    [
        ("mqtt_password", {"mqtt_enabled": True, "mqtt_username": "account"}),
        ("ha_token", {"ha_enabled": True}),
        ("ldap_bind_password", {"ldap_enabled": True, "ldap_bind_dn": "cn=account"}),
        ("prometheus_token", {"prometheus_enabled": True}),
        ("virtual_printer_access_code", {"virtual_printer_enabled": True}),
    ],
)
async def test_required_clear_refuses_without_writes(db_session, monkeypatch, field, enabled):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await seed(db_session, {**enabled, field: "12345678"})
    for changes, actions in [({}, {field: "clear"}), ({field: ""}, None)]:
        with pytest.raises(HTTPException) as error:
            await resolve(db_session, changes, actions)
        assert error.value.status_code == 400
    assert await settings.get_setting(db_session, field) == "12345678"


async def test_environment_pair_precedence_never_copies_env_secret(db_session, monkeypatch):
    await seed(db_session, {"ha_url": "http://stored.local/a", "ha_token": "stored-canary"})
    monkeypatch.setenv("HA_TOKEN", "env-canary")
    monkeypatch.delenv("HA_URL", raising=False)
    for change in [{"ha_url": "http://new.local"}, {"ha_url": "http://new.local", "ha_token": "new-db-token"}]:
        with pytest.raises(HTTPException) as error:
            await resolve(db_session, change)
        assert error.value.status_code == 400
    monkeypatch.setenv("HA_URL", "http://environment.local/base")
    merged = await resolve(db_session, {"ha_url": "http://new-stored.local"})
    assert merged["ha_url"] == "http://new-stored.local"
    assert merged["ha_token"] == "stored-canary"
    raw = await settings.get_homeassistant_settings(db_session)
    assert (raw["ha_url"], raw["ha_token"]) == ("http://environment.local/base", "env-canary")
    assert await settings.get_setting(db_session, "ha_token") == "stored-canary"


SMTP = {
    "smtp_host": "mail.local",
    "smtp_port": 587,
    "smtp_username": "account",
    "smtp_password": "smtp-canary",
    "smtp_security": "starttls",
    "smtp_auth_enabled": True,
    "smtp_from_email": "from@example.org",
}


async def test_smtp_read_and_saved_actions(async_client, db_session):
    await seed(db_session, SMTP)
    response = await async_client.get("/api/v1/auth/smtp")
    assert response.status_code == 200
    assert response.json()["smtp_password_configured"] is True
    assert "smtp_password" not in response.json()
    for legacy_blank in ["", None]:
        result = await auth._resolve_smtp_draft(
            db_session, SMTPSettingsUpdate(**{**SMTP, "smtp_password": legacy_blank})
        )
        assert result.smtp_password == SMTP["smtp_password"]
    for changed in [
        {"smtp_host": "new.local"},
        {"smtp_port": 465},
        {"smtp_security": "none"},
        {"smtp_username": "other"},
        {"smtp_use_tls": False},
    ]:
        body = {**SMTP, **changed}
        body.pop("smtp_password")
        if "smtp_use_tls" in changed:
            body.pop("smtp_security")
        with pytest.raises(HTTPException) as error:
            await auth._resolve_smtp_draft(db_session, SMTPSettingsUpdate(**body))
        assert error.value.status_code == 400
    body = {key: value for key, value in SMTP.items() if key != "smtp_password"}
    await db_session.rollback()
    response = await async_client.post(
        "/api/v1/auth/smtp", json={**body, "smtp_auth_enabled": False, "secret_actions": {"smtp_password": "clear"}}
    )
    assert response.status_code == 200, response.text
    assert await settings.get_setting(db_session, "smtp_password") == ""


async def test_smtp_draft_releases_transaction_before_outbound(db_session, monkeypatch):
    await seed(db_session, SMTP)
    sent = []

    def send(**kwargs):
        assert not db_session.in_transaction()
        sent.append(kwargs["smtp_settings"])

    monkeypatch.setattr(auth, "send_email", send)
    draft = {
        **SMTP,
        "smtp_host": "draft.local",
        "smtp_password": "draft-canary",
        "secret_actions": {"smtp_password": "replace"},
    }
    result = await auth.test_smtp_connection(
        SMTPProbe(test_recipient="to@example.org", settings=draft), None, db_session
    )
    assert result.success and sent[0].smtp_host == "draft.local"
    assert await settings.get_setting(db_session, "smtp_host") == SMTP["smtp_host"]
    assert await settings.get_setting(db_session, "smtp_password") == SMTP["smtp_password"]


async def test_ldap_draft_releases_transaction_before_outbound(db_session, monkeypatch):
    from backend.app.services import ldap_service

    await seed(db_session, {"ldap_server_url": "ldap://old.local", "ldap_bind_password": "old-canary"})
    sent = []

    def send(config):
        assert not db_session.in_transaction()
        sent.append(config)
        return True, "ok"

    monkeypatch.setattr(ldap_service, "test_ldap_connection", send)
    result = await auth.test_ldap(
        AppSettingsUpdate(
            ldap_server_url="ldap://draft.local",
            ldap_bind_password="draft-canary",
            secret_actions={"ldap_bind_password": "replace"},
        ),
        None,
        db_session,
    )
    assert result["success"] and sent[0].server_url == "ldap://draft.local"
    assert await settings.get_setting(db_session, "ldap_bind_password") == "old-canary"


async def test_obico_draft_releases_transaction_and_does_not_reuse_at_new_destination(db_session, monkeypatch):
    await seed(db_session, {"obico_ml_url": "http://old.local", "obico_ml_token": "old-canary"})

    async def send(url, token):
        assert not db_session.in_transaction()
        assert (url, token) == ("http://draft.local", "draft-canary")
        return {"ok": True}

    probe = AsyncMock(side_effect=send)
    monkeypatch.setattr(obico.obico_detection_service, "test_connection", probe)
    with pytest.raises(HTTPException) as error:
        await obico.test_connection(obico.TestConnectionRequest(url="http://draft.local"), None, db_session)
    assert error.value.status_code == 400
    probe.assert_not_called()
    result = await obico.test_connection(
        obico.TestConnectionRequest(url="http://draft.local", token="draft-canary"), None, db_session
    )
    assert result["ok"]
    assert await settings.get_setting(db_session, "obico_ml_token") == "old-canary"


async def test_mixed_mcp_and_credentials_uses_single_sqlite_writer(async_client, db_session):
    await seed(db_session, {"mqtt_broker": "old.local", "mqtt_password": "old-canary"})
    response = await async_client.put(
        "/api/v1/settings/",
        json={
            "mqtt_broker": "new.local",
            "mqtt_password": "new-canary",
            "secret_actions": {"mqtt_password": "replace"},
            "mcp_allow_http": False,
            "mcp_endpoint_url": "https://mcp.local/mcp",
        },
    )
    assert response.status_code == 200, response.text
    assert await settings.get_setting(db_session, "mqtt_password") == "new-canary"


async def test_global_vp_json_clear_does_not_silently_disable(async_client, db_session):
    await seed(db_session, {"virtual_printer_enabled": True, "virtual_printer_access_code": "12345678"})
    response = await async_client.put(
        "/api/v1/settings/virtual-printer", json={"secret_actions": {"access_code": "clear"}}
    )
    assert response.status_code == 400, response.text
    assert await settings.get_setting(db_session, "virtual_printer_enabled") == "true"
    assert await settings.get_setting(db_session, "virtual_printer_access_code") == "12345678"
    response = await async_client.put("/api/v1/settings/virtual-printer?access_code=canary", json={})
    assert response.status_code == 422 and "canary" not in response.text


async def test_delegated_editor_cannot_submit_read_blanks_as_secret_noops(async_client, db_session):
    from backend.app.core.auth import create_access_token
    from backend.app.models.group import Group
    from backend.app.models.user import User

    group = Group(name="B delegated settings", permissions=["settings:read", "settings:update"])
    user = User(username="b-delegated", password_hash="unused", role="user", is_active=True, groups=[group])
    db_session.add(user)
    await db_session.commit()
    await seed(db_session, dict.fromkeys(settings.SETTINGS_SECRETS, "12345678"))
    headers = {"Authorization": "Bearer " + await mint_test_access(db_session, username=user.username)}
    for field in settings.SETTINGS_SECRETS:
        response = await async_client.put("/api/v1/settings/", headers=headers, json={field: "", "currency": "EUR"})
        assert response.status_code == 403, response.text
    assert await settings.get_setting(db_session, "currency") is None


async def test_mqtt_runtime_application_has_no_transaction_or_mixed_pair(async_client, monkeypatch):
    from backend.app.services.mqtt_relay import mqtt_relay

    snapshots = []
    original = settings._load_settings

    async def load(db, **kwargs):
        snapshots.append(db)
        return await original(db, **kwargs)

    async def configure(snapshot):
        assert not snapshots[-1].in_transaction()
        assert (snapshot["mqtt_broker"], snapshot["mqtt_password"]) == ("draft.local", "runtime-canary")
        return True

    monkeypatch.setattr(settings, "_load_settings", load)
    send = AsyncMock(side_effect=configure)
    monkeypatch.setattr(mqtt_relay, "configure", send)
    response = await async_client.put(
        "/api/v1/settings/", json={"mqtt_broker": "draft.local", "mqtt_password": "runtime-canary"}
    )
    assert response.status_code == 200, response.text
    send.assert_awaited_once()


async def test_overlapping_mqtt_applies_finish_in_committed_snapshot_order(async_client, monkeypatch):
    from backend.app.services.mqtt_relay import mqtt_relay

    entered, queued, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = asyncio.Lock()

    class ObservedLock:
        async def __aenter__(self):
            if lock.locked():
                queued.set()
            await lock.acquire()

        async def __aexit__(self, *args):
            lock.release()

    monkeypatch.setattr(settings, "_mqtt_apply_lock", ObservedLock())
    completed = []

    async def configure(snapshot):
        pair = (snapshot["mqtt_broker"], snapshot["mqtt_password"])
        if pair[0] == "first.local":
            entered.set()
            await release.wait()
        completed.append(pair)
        return True

    monkeypatch.setattr(mqtt_relay, "configure", configure)
    first = asyncio.create_task(
        async_client.put("/api/v1/settings/", json={"mqtt_broker": "first.local", "mqtt_password": "first-secret"})
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(
            async_client.put(
                "/api/v1/settings/", json={"mqtt_broker": "second.local", "mqtt_password": "second-secret"}
            )
        )
        await asyncio.wait_for(queued.wait(), 10)  # Second writer committed, then waited for service application.
        release.set()
        responses = await asyncio.gather(first, second)
        assert all(response.status_code == 200 for response in responses)
        assert completed == [("first.local", "first-secret"), ("second.local", "second-secret")]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_reset_reconfigures_mqtt_with_cleared_snapshot_and_preserves_anchor(
    async_client, db_session, monkeypatch
):
    from backend.app.core.credential_config import CONFIG_GUARD_KEY
    from backend.app.services.mqtt_relay import mqtt_relay

    await seed(db_session, {"mqtt_enabled": True, "mqtt_broker": "old.local", "mqtt_password": "old-canary"})
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(mqtt_relay, "configure", send)
    response = await async_client.post("/api/v1/settings/reset")
    assert response.status_code == 200, response.text
    snapshot = send.call_args.args[0]
    assert snapshot["mqtt_enabled"] is False
    assert snapshot["mqtt_password"] == "" and snapshot["mqtt_broker"] == ""
    assert await settings.get_setting(db_session, CONFIG_GUARD_KEY) is not None
    assert response.json()["mqtt_password_configured"] is False
    assert "mqtt_password" not in response.json()


@pytest.mark.parametrize("field", ["ha_url", "obico_ml_url", "ldap_server_url"])
async def test_legacy_opaque_urls_are_private_and_metadata_edit_preserves_exact_raw_value(
    async_client, db_session, field
):
    prefix = "ldap" if field == "ldap_server_url" else "http"
    raw = f"{prefix}://user:opaque-url-canary@old.local/base?access_token=opaque-query-canary"
    companion = {"ha_url": "ha_token", "obico_ml_url": "obico_ml_token", "ldap_server_url": "ldap_bind_password"}[field]
    await seed(db_session, {field: raw, companion: "companion-canary"})
    response = await async_client.get("/api/v1/settings/")
    assert response.status_code == 200, response.text
    assert "canary" not in response.text
    assert response.json()[field] == ""
    assert response.json()[f"{field}_configured"] and response.json()[f"{field}_opaque"]
    response = await async_client.put("/api/v1/settings/", json={"currency": "EUR", "secret_actions": {field: "keep"}})
    assert response.status_code == 200, response.text
    assert "canary" not in response.text
    assert await settings.get_setting(db_session, field) == raw
    await db_session.rollback()
    response = await async_client.put(
        "/api/v1/settings/",
        json={
            field: f"{prefix}://new.local/base",
            companion: "replacement-canary",
            "secret_actions": {field: "replace", companion: "replace"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()[field] == f"{prefix}://new.local/base"
    assert response.json()[f"{field}_opaque"] is False


@pytest.mark.parametrize("field", ["ha_url", "obico_ml_url", "ldap_server_url"])
@pytest.mark.parametrize(
    "suffix",
    ["user:embedded-canary@host.local/base", "host.local/base?token=embedded-canary", "host.local/#embedded-canary"],
)
async def test_new_base_url_credentials_are_rejected_without_echo(async_client, field, suffix):
    scheme = "ldap" if field == "ldap_server_url" else "http"
    response = await async_client.put("/api/v1/settings/", json={field: f"{scheme}://{suffix}"})
    assert response.status_code == 422, response.text
    assert "embedded-canary" not in response.text


async def test_url_secret_action_is_rejected_for_ordinary_nonsecret_destination(async_client, db_session):
    await seed(db_session, {"ha_url": "http://safe.local"})
    response = await async_client.put("/api/v1/settings/", json={"secret_actions": {"ha_url": "keep"}})
    assert response.status_code == 422, response.text


async def test_env_only_ha_token_explicit_keep_is_not_materialized(db_session, monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "environment-only-canary")
    monkeypatch.setenv("HA_URL", "http://environment.local")
    merged = await resolve(db_session, {}, {"ha_token": "keep"})
    assert merged["ha_token"] == ""
    assert await settings.get_setting(db_session, "ha_token") is None


async def test_obico_status_redacts_opaque_legacy_url(async_client, monkeypatch):
    raw = "http://opaque-status-canary:password@ml.local/base?token=opaque-query-canary"
    monkeypatch.setattr(
        obico.obico_detection_service,
        "_load_settings",
        AsyncMock(
            return_value={
                "enabled": False,
                "ml_url": raw,
                "sensitivity": "medium",
                "action": "notify",
                "poll_interval": 10,
                "external_url": "",
                "save_detections": False,
            }
        ),
    )
    monkeypatch.setattr(
        obico.obico_detection_service, "get_status", lambda *args, **kwargs: {"per_printer": {}, "history": []}
    )
    monkeypatch.setattr(obico.obico_history, "status", lambda: {})
    response = await async_client.get("/api/v1/obico/status")
    assert response.status_code == 200, response.text
    assert response.json()["ml_url"] == "" and response.json()["ml_url_configured"]
    assert "canary" not in response.text


async def test_independent_smartplug_apply_uses_latest_raw_pair_without_enabling_publisher(db_session, monkeypatch):
    from backend.app.services.mqtt_relay import mqtt_relay

    await seed(db_session, {"mqtt_enabled": False, "mqtt_broker": "latest.local", "mqtt_password": "latest-canary"})

    async def configure(snapshot):
        assert not db_session.in_transaction()
        assert snapshot["mqtt_enabled"] is True
        assert (snapshot["mqtt_broker"], snapshot["mqtt_password"]) == ("latest.local", "latest-canary")

    send = AsyncMock(side_effect=configure)
    monkeypatch.setattr(mqtt_relay.smart_plug_service, "configure", send)
    await settings._apply_mqtt_runtime(db_session, smart_plug_only=True)
    send.assert_awaited_once()
    assert await settings.get_setting(db_session, "mqtt_enabled") == "false"


@pytest.mark.parametrize(("field", "key"), [("token", "obico_ml_token"), ("url", "obico_ml_url")])
@pytest.mark.parametrize("action", ["keep", "clear"])
async def test_obico_explicit_keep_clear_cannot_supply_null(field, key, action, async_client, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(obico.obico_detection_service, "test_connection", send)
    response = await async_client.post(
        "/api/v1/obico/test-connection", json={field: None, "secret_actions": {key: action}}
    )
    assert response.status_code == 422, response.text
    send.assert_not_called()


async def test_global_vp_mode_switch_preserves_existing_safe_auto_disable(async_client, db_session, monkeypatch):
    from backend.app.services.virtual_printer import virtual_printer_manager

    await seed(db_session, {"virtual_printer_enabled": True, "virtual_printer_mode": "proxy"})
    monkeypatch.setattr(virtual_printer_manager, "configure", AsyncMock())
    response = await async_client.put("/api/v1/settings/virtual-printer", json={"mode": "file_manager"})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert await settings.get_setting(db_session, "virtual_printer_enabled") == "false"


@pytest.mark.parametrize("success", [True, False])
async def test_rs1_ldap_success_and_failure_do_not_echo_driver_secrets(db_session, monkeypatch, caplog, success):
    from backend.app.services import ldap_service

    url = "ldap://account:url-canary@directory.local/base?token=query-canary"
    await seed(db_session, {"ldap_server_url": url, "ldap_bind_password": "bind-canary"})

    def send(config):
        assert not db_session.in_transaction()
        assert config.server_url == url and config.bind_password == "bind-canary"
        return success, f"driver echo {url} bind-canary"

    monkeypatch.setattr(ldap_service, "test_ldap_connection", send)
    result = await auth.test_ldap(None, None, db_session)
    assert result["success"] is success
    assert "canary" not in str(result) and "canary" not in caplog.text
    assert await settings.get_setting(db_session, "ldap_server_url") == url
    assert await settings.get_setting(db_session, "ldap_bind_password") == "bind-canary"


@pytest.mark.parametrize("success", [True, False])
async def test_rs2_obico_success_and_failure_project_typed_health_without_peer_secrets(
    db_session, monkeypatch, success
):
    url = "http://account:url-canary@ml.local/base?token=query-canary"
    await seed(db_session, {"obico_ml_url": url, "obico_ml_token": "ml-canary"})

    async def send(destination, token):
        assert not db_session.in_transaction()
        assert (destination, token) == (url, "ml-canary")
        return {
            "ok": success,
            "auth_ok": success,
            "status_code": 200 if success else 401,
            "body": f"peer echo {url} ml-canary",
            "error": "driver echo ml-canary",
        }

    monkeypatch.setattr(obico.obico_detection_service, "test_connection", send)
    result = await obico.test_connection(obico.TestConnectionRequest(), None, db_session)
    assert result["ok"] is success and result["auth_ok"] is success
    assert result["status_code"] == (200 if success else 401)
    assert result["body"] == ("ok" if success else None)
    assert "canary" not in str(result)
    assert await settings.get_setting(db_session, "obico_ml_url") == url
    assert await settings.get_setting(db_session, "obico_ml_token") == "ml-canary"


async def test_rs2_live_obico_diagnostics_keep_categories_without_legacy_url_credentials(async_client, monkeypatch):
    raw = "ML API call failed for printer 1: http://account:url-canary@ml.local/base?token=query-canary"
    per_printer = {1: {"class": "error", "error": raw, "frame_count": 0, "score": 0}}
    monkeypatch.setattr(
        obico.obico_detection_service,
        "_load_settings",
        AsyncMock(
            return_value={
                "enabled": True,
                "ml_url": "http://ml.local",
                "ml_token": "ml-canary",
                "enabled_printers": None,
                "sensitivity": "medium",
                "action": "notify",
                "poll_interval": 10,
                "external_url": "",
                "save_detections": False,
            }
        ),
    )
    monkeypatch.setattr(obico.obico_detection_service, "_last_error", raw)
    monkeypatch.setattr(obico.obico_detection_service, "get_per_printer", lambda: per_printer)
    monkeypatch.setattr(
        obico.obico_detection_service,
        "get_status",
        lambda *args, **kwargs: {"last_error": raw, "per_printer": per_printer, "history": []},
    )
    monkeypatch.setattr(obico.obico_history, "status", lambda: {})
    for path in ("/api/v1/obico/status", "/api/v1/obico/printer-status"):
        response = await async_client.get(path)
        assert response.status_code == 200, response.text
        assert "canary" not in response.text
        body = response.json()
        assert body["last_error"].startswith("ML API call failed for printer 1.")
        assert body["per_printer"]["1"]["class"] == "error"
        assert body["per_printer"]["1"]["error"].startswith("ML API call failed for printer 1.")


@pytest.mark.parametrize(
    ("field", "secret", "old_value"),
    [("mqtt_port", "mqtt_password", "1883"), ("ha_url", "ha_token", "http://ha.local")],
)
async def test_rs3_invalid_effective_destination_refuses_before_any_write_or_runtime(
    async_client, db_session, monkeypatch, field, secret, old_value
):
    await seed(db_session, {field: old_value, secret: "old-canary"})
    send = AsyncMock()
    monkeypatch.setattr(settings, "_apply_mqtt_runtime", send)
    response = await async_client.put(
        "/api/v1/settings/", json={field: None, secret: "replacement-canary", "secret_actions": {secret: "replace"}}
    )
    assert response.status_code == 422, response.text
    assert "canary" not in response.text
    send.assert_not_awaited()
    assert await settings.get_setting(db_session, field) == old_value
    assert await settings.get_setting(db_session, secret) == "old-canary"
    await db_session.rollback()
    read = await async_client.get("/api/v1/settings/")
    assert read.status_code == 200, read.text


@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("with_ca", [False, True])
async def test_ba1_ldap_test_preserves_runtime_tls_verification(db_session, monkeypatch, tmp_path, draft, with_ca):
    import ssl

    from backend.app.services import ldap_service

    ca = tmp_path / "directory-ca.pem"
    ca.write_text("operator-managed-certificate")
    await seed(
        db_session,
        {
            "ldap_enabled": True,
            "ldap_server_url": "ldaps://directory.local",
            "ldap_bind_password": "bind-canary",
            "ldap_ca_cert_path": str(ca) if with_ca else "",
        },
    )
    runtime = ldap_service.parse_ldap_config(await auth._get_ldap_settings(db_session))
    await db_session.rollback()
    captured = []

    def send(config):
        assert not db_session.in_transaction()
        captured.append(config)
        server = ldap_service._create_server(config)
        assert config.ca_cert_path == runtime.ca_cert_path
        assert server.tls.validate == (ssl.CERT_REQUIRED if with_ca else ssl.CERT_NONE)
        assert server.tls.ca_certs_file == (str(ca) if with_ca else None)
        return True, "ok"

    monkeypatch.setattr(ldap_service, "test_ldap_connection", send)
    result = await auth.test_ldap(AppSettingsUpdate(ldap_search_base="dc=draft") if draft else None, None, db_session)
    assert result["success"] and len(captured) == 1
    assert captured[0].bind_password == "bind-canary"
    assert await settings.get_setting(db_session, "ldap_search_base") is None


@pytest.mark.parametrize("scheme,expected_status", [("LDAPS", 200), ("LdApS", 200), ("foo", 422)])
async def test_rb1_saved_ldap_test_matches_login_scheme_handling(
    async_client, db_session, monkeypatch, scheme, expected_status
):
    from backend.app.services import ldap_service

    url = f"{scheme}://directory.local"
    await seed(db_session, {"ldap_server_url": url, "ldap_bind_password": "bind-canary"})
    captured = []

    def send(config):
        captured.append(ldap_service._create_server(config))
        return True, "ok"

    monkeypatch.setattr(ldap_service, "test_ldap_connection", send)
    response = await async_client.post("/api/v1/auth/ldap/test")
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert response.json()["success"]
        assert len(captured) == 1
        assert (captured[0].host, captured[0].port, captured[0].ssl) == ("directory.local", 636, True)
        config = {"ldap_server_url": url, "ldap_security": "starttls"}
        assert settings._ldap_credential_scope(config) == settings._ldap_credential_scope(
            {**config, "ldap_server_url": "ldaps://directory.local"}
        )
    else:
        assert not captured
    assert await settings.get_setting(db_session, "ldap_server_url") == url
    assert await settings.get_setting(db_session, "ldap_bind_password") == "bind-canary"


async def test_ba8_delegated_secret_guesses_always_forbidden_including_equal_and_absent(async_client, db_session):
    from backend.app.core.auth import create_access_token
    from backend.app.models.group import Group
    from backend.app.models.user import User

    user = User(
        username="ba8-delegated",
        role="user",
        is_active=True,
        groups=[Group(name="BA8 settings editor", permissions=["settings:read", "settings:update"])],
    )
    db_session.add(user)
    await db_session.commit()
    await seed(db_session, {key: "12345678" for key in settings.SETTINGS_SECRETS if key != "ha_token"})
    headers = {"Authorization": "Bearer " + await mint_test_access(db_session, username=user.username)}
    for field in settings.SETTINGS_SECRETS:
        for value in ["wrong-guess", "12345678", "", None]:
            response = await async_client.put(
                "/api/v1/settings/", headers=headers, json={field: value, "currency": "EUR"}
            )
            assert response.status_code == 403, (field, value, response.text)
    opaque = "http://user:opaque-canary@legacy.local"
    await seed(db_session, dict.fromkeys(settings.SERVICE_CREDENTIAL_URL_FIELDS, opaque))
    for field in settings.SERVICE_CREDENTIAL_URL_FIELDS:
        for value in [opaque, "http://wrong.local", ""]:
            response = await async_client.put(
                "/api/v1/settings/", headers=headers, json={field: value, "currency": "EUR"}
            )
            # The schema rejects credential-bearing base URL literals for
            # every caller before dispatch; valid guesses all hit the 403 gate.
            assert response.status_code == (422 if value == opaque else 403), (field, response.text)
            assert "canary" not in response.text
    assert await settings.get_setting(db_session, "currency") is None


async def test_ba9_metrics_cannot_reenable_after_disabled_clear_or_enable_with_blank(async_client, db_session):
    await seed(db_session, {"prometheus_enabled": True, "prometheus_token": "metrics-canary"})
    cleared = await async_client.put(
        "/api/v1/settings/", json={"prometheus_enabled": False, "secret_actions": {"prometheus_token": "clear"}}
    )
    assert cleared.status_code == 200, cleared.text
    for payload in [{"prometheus_enabled": True}, {"prometheus_enabled": True, "prometheus_token": "   "}]:
        response = await async_client.put("/api/v1/settings/", json=payload)
        assert response.status_code == 400, response.text
        assert "canary" not in response.text
    assert await settings.get_setting(db_session, "prometheus_enabled") == "false"
    assert await settings.get_setting(db_session, "prometheus_token") == ""
    await db_session.rollback()
    enabled = await async_client.put(
        "/api/v1/settings/",
        json={
            "prometheus_enabled": True,
            "prometheus_token": "new-canary",
            "secret_actions": {"prometheus_token": "replace"},
        },
    )
    assert enabled.status_code == 200, enabled.text


async def test_ba10_legacy_ldap_unknown_scheme_allows_metadata_but_new_and_test_refuse(
    async_client, db_session, monkeypatch
):
    from backend.app.services import ldap_service

    await seed(db_session, {"ldap_server_url": "foo://directory.local", "ldap_bind_password": "bind-canary"})
    probe = AsyncMock()
    monkeypatch.setattr(ldap_service, "test_ldap_connection", probe)
    response = await async_client.put(
        "/api/v1/settings/",
        json={"ldap_server_url": "foo://directory.local", "ldap_default_group": "Viewers", "currency": "EUR"},
    )
    assert response.status_code == 200, response.text
    for payload in [
        {"ldap_server_url": "foo://other.local", "ldap_bind_password": "new-canary"},
        {"ldap_server_url": "ldap://host.local:not-a-port", "ldap_bind_password": "new-canary"},
    ]:
        response = await async_client.put("/api/v1/settings/", json=payload)
        assert 400 <= response.status_code < 500, response.text
        assert "canary" not in response.text
    response = await async_client.post("/api/v1/auth/ldap/test")
    assert response.status_code == 422, response.text
    probe.assert_not_called()
    assert await settings.get_setting(db_session, "ldap_server_url") == "foo://directory.local"
    assert await settings.get_setting(db_session, "ldap_bind_password") == "bind-canary"


async def test_ba11_auth_smtp_auth_off_retains_password_without_transmitting_or_rebinding(
    async_client, db_session, monkeypatch
):
    from unittest.mock import MagicMock

    from backend.app.services import email_service

    await seed(db_session, SMTP)
    body = {key: value for key, value in SMTP.items() if key != "smtp_password"}
    sender = MagicMock()
    monkeypatch.setattr(email_service.smtplib, "SMTP", sender)

    def send(**kwargs):
        assert not db_session.in_transaction()
        assert not kwargs["smtp_settings"].smtp_auth_enabled
        assert kwargs["smtp_settings"].smtp_password == SMTP["smtp_password"]
        email_service.send_email(**kwargs)

    monkeypatch.setattr(auth, "send_email", send)
    draft_result = await auth.test_smtp_connection(
        SMTPProbe(test_recipient="recipient@example.org", settings={**body, "smtp_auth_enabled": False}),
        None,
        db_session,
    )
    assert draft_result.success
    assert await settings.get_setting(db_session, "smtp_auth_enabled") == "true"
    await db_session.rollback()
    response = await async_client.post("/api/v1/auth/smtp", json={**body, "smtp_auth_enabled": False})
    assert response.status_code == 200, response.text
    saved = await email_service.get_smtp_settings(db_session)
    assert not saved.smtp_auth_enabled and saved.smtp_password == SMTP["smtp_password"]
    assert saved.smtp_username == SMTP["smtp_username"]
    await db_session.rollback()
    email_service.send_email(saved, "recipient@example.org", "Test", "Body")
    sender.return_value.__enter__.return_value.login.assert_not_called()
    assert sender.return_value.__enter__.return_value.send_message.call_count == 2
    for payload in [{"smtp_host": "other.local"}, {"smtp_username": "other"}]:
        response = await async_client.post("/api/v1/auth/smtp", json={**body, "smtp_auth_enabled": False, **payload})
        assert response.status_code == 400, response.text
    response = await async_client.post("/api/v1/auth/smtp", json={**body, "smtp_auth_enabled": True})
    assert response.status_code == 200, response.text
