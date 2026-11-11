"""Real sender errors redact known capability material before truncation."""

from types import SimpleNamespace
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


@pytest.mark.parametrize("kind,field", [("ntfy", "auth_token"), ("webhook", "auth_header"), ("signal", "auth_header")])
def test_whitespace_auth_cannot_implicitly_select_anonymous(kind, field):
    old = CASES[kind]
    safe, _, _ = provider_projection(kind, old)
    with pytest.raises(HTTPException) as failure:
        resolve_provider_config(kind, {**safe, field: " \t "}, None, stored=old, stored_type=kind)
    assert failure.value.status_code == 422
    anonymous = resolve_provider_config(
        kind, {**safe, field: " \t ", "auth_mode": "anonymous"}, None, stored=old, stored_type=kind
    )
    assert anonymous[field] == ""
    replacement = resolve_provider_config(
        kind, {**safe, field: "  CANARY_NEW_AUTH  "}, None, stored=old, stored_type=kind
    )
    assert replacement[field] == "CANARY_NEW_AUTH"


def test_short_ordinary_url_path_segments_do_not_corrupt_diagnostics():
    config = {"webhook_url": "https://hooks.example/print/a", "auth_header": "Bearer CANARY_AUTH_LONG"}
    prose = "Your print finished on a printer named Alpha"
    assert safe_provider_message(prose, "webhook", config) == prose
    assert safe_provider_message("HTTP 500: a printer failed", "webhook", config) == "HTTP 500: a printer failed"
    assert "https://hooks.example" not in safe_provider_message(config["webhook_url"], "webhook", config)


@pytest.mark.parametrize(
    "field,value", [("auth_token", "  CANARY_TOKEN  "), ("auth_header", "  Bearer CANARY_TOKEN  ")]
)
def test_known_auth_redaction_includes_sender_trimmed_legacy_value(field, value):
    kind = "ntfy" if field == "auth_token" else "webhook"
    assert "CANARY" not in safe_provider_message("peer rejected CANARY_TOKEN", kind, {field: value})
    assert (
        safe_provider_message("ordinary diagnostics", "webhook", {"auth_header": "Bearer "}) == "ordinary diagnostics"
    )


def test_email_auth_can_turn_off_without_rebinding_or_discarding_password():
    old = CASES["email"]
    safe, _, _ = provider_projection("email", old)
    off = resolve_provider_config("email", {**safe, "auth_enabled": "false"}, None, stored=old, stored_type="email")
    assert off["password"] == old["password"] and off["username"] == old["username"]
    on = resolve_provider_config("email", {**safe, "auth_enabled": "true"}, None, stored=off, stored_type="email")
    assert on["password"] == old["password"]
    for changes in ({"smtp_server": "other.example"}, {"username": "other"}, {"security": "none"}):
        with pytest.raises(HTTPException) as failure:
            resolve_provider_config(
                "email", {**safe, "auth_enabled": "false", **changes}, None, stored=off, stored_type="email"
            )
        assert failure.value.status_code == 400


@pytest.mark.parametrize("server", ["", None])
async def test_ntfy_nonworking_empty_server_cannot_rebind_kept_secrets_to_public_default(server, monkeypatch):
    old = {**CASES["ntfy"], "server": server}
    with pytest.raises(HTTPException) as failure:
        resolve_provider_config("ntfy", {"server": "https://ntfy.sh"}, None, stored=old, stored_type="ntfy")
    assert failure.value.status_code == 400
    service = NotificationService()
    client = AsyncMock()
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    success, _ = await service.send_test_notification("ntfy", old)
    assert not success
    client.post.assert_not_awaited()


async def test_ntfy_missing_server_default_scope_matches_actual_sender(monkeypatch):
    old = {key: value for key, value in CASES["ntfy"].items() if key != "server"}
    config = resolve_provider_config("ntfy", {"server": "https://ntfy.sh/"}, None, stored=old, stored_type="ntfy")
    service = NotificationService()
    client = AsyncMock()
    client.post.return_value = httpx.Response(200)
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    success, _ = await service.send_test_notification("ntfy", config)
    assert success
    assert client.post.call_args.args[0] == "https://ntfy.sh/" + old["topic"]


async def test_email_auth_off_never_transmits_retained_password(monkeypatch):
    sent, authenticated = [], []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def starttls(self):
            pass

        def login(self, username, password):
            authenticated.append((username, password))

        def sendmail(self, *args):
            sent.append(args)

        def quit(self):
            pass

    monkeypatch.setattr("backend.app.services.notification_service.smtplib.SMTP", SMTP)
    config = resolve_provider_config(
        "email",
        {**CASES["email"], "auth_enabled": "false", "password": "CANARY_PASSWORD"},
        None,
        stored=CASES["email"],
        stored_type="email",
    )
    success, _ = await NotificationService().send_test_notification("email", config)
    assert success and sent and not authenticated
    assert "CANARY_PASSWORD" not in str(sent)


@pytest.mark.parametrize("image", [None, b"image"])
async def test_telegram_bot_swap_during_keyboard_work_cannot_change_frozen_token(image, monkeypatch):
    from backend.app.services import telegram_bot

    service = NotificationService()
    live = [SimpleNamespace(token="111:CANARY_FROZEN")]
    monkeypatch.setattr(telegram_bot, "get_bot", lambda provider_id: live[0])

    async def build_actions(*args):
        live[0] = SimpleNamespace(token="222:CANARY_DIFFERENT")

    monkeypatch.setattr(service, "_build_telegram_actions", build_actions)
    message, photo = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr(telegram_bot, "send_message", message)
    monkeypatch.setattr(telegram_bot, "send_photo", photo)
    client = AsyncMock()
    client.post.return_value = httpx.Response(401, text="rejected CANARY_FROZEN")
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    success, error = await service._send_telegram(
        {"bot_token": "111:CANARY_FROZEN", "chat_id": "42"}, "test", image_data=image, provider_id=7
    )
    assert not success and "CANARY" not in error
    message.assert_not_awaited()
    photo.assert_not_awaited()
    assert client.post.await_count == 1
    method = "sendPhoto" if image else "sendMessage"
    assert client.post.call_args.args[0] == "https://api.telegram.org/bot111:CANARY_FROZEN/" + method


@pytest.mark.parametrize("image", [None, b"image"])
async def test_telegram_helper_driver_error_never_enters_raw_log_record(image, monkeypatch, caplog):
    from backend.app.services import telegram_bot

    token = "111:CANARY_AI_DRIVER"
    bot = SimpleNamespace(
        token=token,
        send_message=AsyncMock(side_effect=RuntimeError("peer echoed " + token)),
        send_photo=AsyncMock(side_effect=RuntimeError("peer echoed " + token)),
    )
    monkeypatch.setattr(telegram_bot, "_bots", {7: bot})
    service = NotificationService()
    monkeypatch.setattr(service, "_build_telegram_actions", AsyncMock(return_value=None))
    client = AsyncMock()
    client.post.return_value = httpx.Response(401, text="rejected " + token)
    monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    success, error = await service._send_telegram(
        {"bot_token": token, "chat_id": "42"}, "test", image_data=image, provider_id=7
    )
    assert not success and "CANARY" not in error
    assert any("Failed to send Telegram" in record.getMessage() for record in caplog.records)
    assert all("CANARY" not in record.getMessage() and "CANARY" not in str(record.args) for record in caplog.records)
