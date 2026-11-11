"""Real sender errors redact known capability material before truncation."""

from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from fastapi import HTTPException

from backend.app.services.notification_credentials import (
    provider_projection,
    resolve_provider_config,
    safe_provider_message,
)
from backend.app.services.notification_service import NotificationService
from backend.tests.integration.test_g01_b_notification_secrets import CASES


@pytest.mark.parametrize("kind", [kind for kind in CASES if kind not in {"homeassistant", "email"}])
async def test_real_http_sender_error_redacts_before_its_body_limit(kind, monkeypatch):
    config = dict(CASES[kind])
    config["chat_id"] = "42"
    service = NotificationService()
    # A very long credential crosses the sender's 200-char display limit.
    field = {
        "callmebot": "apikey",
        "ntfy": "auth_token",
        "pushover": "app_token",
        "bark": "device_key",
        "telegram": "bot_token",
        "discord": "webhook_url",
        "webhook": "auth_header",
        "signal": "auth_header",
    }[kind]
    base = "CANARY_PRIVATE_" + "x" * 400
    value = {
        "telegram": "123:" + base,
        "discord": "https://discord.com/api/webhooks/123/" + base,
        "webhook": "Bearer " + base,
        "signal": "Bearer " + base,
    }.get(kind, base)
    config[field] = value
    echoed = f"rejected {value} and {quote(value, safe='')}"
    client = AsyncMock()
    response = httpx.Response(400, text=echoed, request=httpx.Request("POST", "https://configured.example"))
    client.get.return_value = client.post.return_value = client.put.return_value = response
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    success, error = await service.send_test_notification(kind, config)
    assert not success and "CANARY" not in error
    assert client.get.await_count + client.post.await_count + client.put.await_count > 0


async def test_ha_driver_exception_uses_effective_global_secret_without_storing_it(monkeypatch):
    token = "CANARY_GLOBAL_HA_TOKEN"
    monkeypatch.setenv("HA_URL", "https://homeassistant.example")
    monkeypatch.setenv("HA_TOKEN", token)
    service = NotificationService()
    client = AsyncMock()
    client.post.side_effect = RuntimeError("driver echoed " + token)
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    config = {"service": "notify.phone"}
    success, error = await service.send_test_notification("homeassistant", config)
    assert not success and token not in error
    assert config == {"service": "notify.phone"}


async def test_smtp_driver_echo_is_sanitized(monkeypatch):
    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            pass

        def login(self, username, password):
            raise RuntimeError("SMTP peer echoed " + password)

    monkeypatch.setattr("backend.app.services.notification_service.smtplib.SMTP", SMTP)
    success, error = await NotificationService().send_test_notification("email", dict(CASES["email"]))
    assert not success and "CANARY_PASSWORD" not in error


def test_known_header_and_capability_fragments_are_not_returned_as_prose():
    config = {
        "webhook_url": "https://host.example/control/CANARY_CAPABILITY?key=CANARY_QUERY",
        "auth_header": "Bearer CANARY_HEADER",
    }
    safe = safe_provider_message("peer echoed CANARY_CAPABILITY CANARY_QUERY CANARY_HEADER", "webhook", config)
    assert "CANARY" not in safe


@pytest.mark.parametrize("kind", ["ntfy", "bark", "signal", "email"])
@pytest.mark.parametrize("url", ["https%3A%2F%2Fuser%3ACANARY%40host", "https://user:CANARY@[", "user:CANARY@host"])
def test_malformed_legacy_endpoint_is_opaque_and_survives_edit(kind, url):
    field = "smtp_server" if kind == "email" else "server"
    config = {**CASES[kind], field: url}
    safe, flags, _ = provider_projection(kind, config)
    assert "CANARY" not in str(safe) and flags[field]
    kept = resolve_provider_config(kind, safe, {field: "keep"}, stored=config, stored_type=kind)
    assert kept[field] == url


async def test_pushover_account_rotation_keeps_app_token_only_at_fixed_api(monkeypatch):
    old = CASES["pushover"]
    config = resolve_provider_config(
        "pushover",
        {"user_key": "CANARY_NEW_USER", "priority": "1"},
        {"user_key": "replace", "app_token": "keep"},
        stored=old,
        stored_type="pushover",
    )
    service = NotificationService()
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={"status": 1})
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    success, _ = await service.send_test_notification("pushover", config)
    assert success
    assert client.post.call_args.args[0] == "https://api.pushover.net/1/messages.json"
    payload = client.post.call_args.kwargs["data"]
    assert payload["token"] == old["app_token"] and payload["user"] == "CANARY_NEW_USER"


@pytest.mark.parametrize("kind,field", [("ntfy", "auth_token"), ("webhook", "auth_header"), ("signal", "auth_header")])
def test_explicit_authentication_mode_agrees_with_credential_state(kind, field):
    old = CASES[kind]
    safe, _, _ = provider_projection(kind, old)
    for changes in ({"auth_mode": "anonymous"}, {"auth_mode": {}}, {"auth_mode": "unknown"}):
        with pytest.raises(HTTPException) as failure:
            resolve_provider_config(kind, {**safe, **changes}, None, stored=old, stored_type=kind)
        assert failure.value.status_code == 422
    resolved = resolve_provider_config(
        kind, {**safe, "auth_mode": "anonymous"}, {field: "clear"}, stored=old, stored_type=kind
    )
    assert not resolved[field]
    no_auth = {**old, field: ""}
    with pytest.raises(HTTPException) as failure:
        resolve_provider_config(kind, {**safe, "auth_mode": "authenticated"}, None, stored=no_auth, stored_type=kind)
    assert failure.value.status_code == 422


@pytest.mark.parametrize("kind,field", [("ntfy", "auth_token"), ("webhook", "auth_header"), ("signal", "auth_header")])
def test_reenable_checks_saved_authentication_mode(kind, field):
    old = {**CASES[kind], field: "", "auth_mode": "authenticated"}
    with pytest.raises(HTTPException) as failure:
        resolve_provider_config(kind, None, None, stored=old, stored_type=kind, validate_required=True)
    assert failure.value.status_code == 422


def test_enabling_email_authentication_requires_a_password():
    old = {**CASES["email"], "password": "", "auth_enabled": "false"}
    with pytest.raises(HTTPException) as failure:
        resolve_provider_config("email", {"auth_enabled": "true"}, None, stored=old, stored_type="email")
    assert failure.value.status_code == 422
