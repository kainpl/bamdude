"""Real notification HTTP doors retain secrets only in their authorized scope."""

import json
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from sqlalchemy import select

from backend.app.api.routes import notifications
from backend.app.core.auth import create_access_token
from backend.app.models.group import Group
from backend.app.models.notification import NotificationLog, NotificationProvider
from backend.app.models.user import User
from backend.app.services import telegram_bot
from backend.app.services.notification_credentials import SECRET_FIELDS, provider_projection
from backend.tests.auth_helpers import mint_test_access

CASES = {
    "callmebot": {"phone": "+15551234567", "apikey": "CANARY_CALLME"},
    "ntfy": {"server": "https://ntfy.example/base", "topic": "CANARY_TOPIC", "auth_token": "CANARY_NTFY"},
    "pushover": {"user_key": "CANARY_USER", "app_token": "CANARY_APP"},
    "bark": {"server": "https://bark.example/base", "device_key": "CANARY_DEVICE"},
    "telegram": {"bot_token": "123456:CANARY_BOT"},
    "email": {
        "smtp_server": "smtp.example",
        "smtp_port": "587",
        "username": "sender",
        "password": "CANARY_PASSWORD",
        "from_email": "from@example.com",
        "to_email": "to@example.com",
    },
    "discord": {"webhook_url": "https://discord.com/api/webhooks/123/CANARY_DISCORD"},
    "webhook": {"webhook_url": "https://hooks.example/CANARY_WEBHOOK", "auth_header": "Bearer CANARY_AUTH"},
    "homeassistant": {"service": "notify.mobile_app_phone", "data": '{"ttl":0}'},
    "signal": {
        "server": "https://signal.example/v2/send",
        "sender_number": "+15551234567",
        "numbers": "+15559876543",
        "auth_header": "Bearer CANARY_SIGNAL",
    },
}


@pytest.fixture(autouse=True)
def no_bot_network(monkeypatch):
    monkeypatch.setattr(telegram_bot, "current_bot_providers", AsyncMock(return_value=[]))
    monkeypatch.setattr(telegram_bot, "restart_telegram_bot", AsyncMock())


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.parametrize("role", ["reader", "editor", "admin"])
async def test_all_types_read_safe_and_runtime_config_retained(
    kind, role, async_client, notification_provider_factory, db_session, monkeypatch
):
    config = CASES[kind]
    provider = await notification_provider_factory(
        provider_type=kind,
        config={**config, "retired_secret": "CANARY_RETIRED"},
        last_error="peer echoed " + " ".join(str(config[k]) for k in SECRET_FIELDS[kind]),
    )
    if role != "admin":
        permissions = ["notifications:read"] + (["notifications:update"] if role == "editor" else [])
        group = Group(name="G01 " + role, permissions=permissions)
        user = User(username="g01_" + role, role="user", is_active=True, groups=[group])
        db_session.add_all([group, user])
        await db_session.commit()
        async_client.headers["Authorization"] = "Bearer " + await mint_test_access(db_session, username=user.username)
    for door in ("/api/v1/notifications/", f"/api/v1/notifications/{provider.id}"):
        response = await async_client.get(door)
        assert response.status_code == 200, response.text
        assert "CANARY" not in response.text
        item = response.json()[0] if isinstance(response.json(), list) else response.json()
        assert item["secrets_configured"] == dict.fromkeys(SECRET_FIELDS[kind], True)
    if role == "reader":
        response = await async_client.post(
            f"/api/v1/notifications/{provider.id}/test-config", json={"provider_type": kind, "config": {}}
        )
        assert response.status_code == 403
        return
    before = provider.config
    safe, _, _ = provider_projection(kind, config)
    draft = AsyncMock(return_value=(False, "peer " + " ".join(str(config[k]) for k in SECRET_FIELDS[kind])))
    monkeypatch.setattr(notifications.notification_service, "send_test_notification", draft)
    response = await async_client.post(
        f"/api/v1/notifications/{provider.id}/test-config", json={"provider_type": kind, "config": safe}
    )
    assert response.status_code == 200, response.text
    assert "CANARY" not in response.text
    passed = draft.call_args.args[1]
    for key in SECRET_FIELDS[kind]:
        assert passed[key] == config[key]
    await db_session.refresh(provider)
    assert provider.config == before  # test never saves the draft
    renamed = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"name": "Renamed", "config": safe})
    assert renamed.status_code == 200, renamed.text
    assert "CANARY" not in renamed.text
    await db_session.refresh(provider)
    for key in SECRET_FIELDS[kind]:
        assert json.loads(provider.config)[key] == config[key]


FIELDS = [(kind, key) for kind, fields in SECRET_FIELDS.items() for key in sorted(fields)]


async def assert_scope_rejected_before_outbound(client, provider_id, kind, config, outbound):
    """Reusable real-HTTP assertion for the PostgreSQL acceptance harness."""
    saved = await client.patch(f"/api/v1/notifications/{provider_id}", json={"config": config})
    assert saved.status_code == 400, saved.text
    tested = await client.post(
        f"/api/v1/notifications/{provider_id}/test-config", json={"provider_type": kind, "config": config}
    )
    assert tested.status_code == 400, tested.text
    outbound.assert_not_awaited()


@pytest.mark.parametrize("kind,field", FIELDS)
async def test_explicit_actions_conflicts_clear_and_replacement(
    kind, field, async_client, notification_provider_factory, db_session
):
    provider = await notification_provider_factory(provider_type=kind, config=CASES[kind])
    door = f"/api/v1/notifications/{provider.id}"
    for body in (
        {"secret_actions": {field: "keep"}, "config": {field: "CANARY_CONFLICT"}},
        {"secret_actions": {field: "replace"}, "config": {field: ""}},
        {"secret_actions": {field: "clear"}, "config": {field: None}},
        {"secret_actions": {field: "replace"}, "config": {field: {"configured": True}}},
        {"secret_actions": {"name": "keep"}},
        {"secret_actions": {field: "bogus"}},
    ):
        response = await async_client.patch(door, json=body)
        assert response.status_code == 422, response.text
        assert "CANARY_CONFLICT" not in response.text
    kept = await async_client.patch(door, json={"secret_actions": {field: "keep"}})
    assert kept.status_code == 200, kept.text
    # Every secret may be cleared in a disabled provider without enabling
    # unauthenticated delivery. A mask-shaped real explicit replacement is real.
    clear_fields = SECRET_FIELDS[kind] if field in {"topic", "webhook_url"} else {field}
    cleared = await async_client.patch(
        door, json={"enabled": False, "secret_actions": dict.fromkeys(clear_fields, "clear")}
    )
    assert cleared.status_code == 200, cleared.text
    missing = await async_client.patch(door, json={"secret_actions": {field: "keep"}})
    assert missing.status_code == 400
    value = "https://discord.com/api/webhooks/123/actual***value" if kind == "discord" else "actual***value"
    replaced = await async_client.patch(
        door, json={"secret_actions": {field: "replace"}, "config": {**CASES[kind], field: value}}
    )
    assert replaced.status_code == 200, replaced.text
    await db_session.refresh(provider)
    assert json.loads(provider.config)[field] == value


@pytest.mark.parametrize(
    "kind,changed",
    [
        ("callmebot", {"phone": "+15550000000"}),
        ("ntfy", {"server": "https://evil.example"}),
        ("ntfy", {"topic": "new-topic"}),
        ("bark", {"server": "https://evil.example"}),
        ("email", {"smtp_server": "evil.example"}),
        ("email", {"security": "none"}),
        ("email", {"username": "other"}),
        ("webhook", {"webhook_url": "https://evil.example/receiver"}),
        ("signal", {"server": "https://evil.example"}),
        ("signal", {"sender_number": "+15550000000"}),
    ],
)
async def test_scope_change_refused_before_save_or_draft_outbound(
    kind, changed, async_client, notification_provider_factory, monkeypatch
):
    provider = await notification_provider_factory(provider_type=kind, config=CASES[kind])
    safe, _, _ = provider_projection(kind, CASES[kind])
    config = {**safe, **changed}
    outbound = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(notifications.notification_service, "send_test_notification", outbound)
    await assert_scope_rejected_before_outbound(async_client, provider.id, kind, config, outbound)


@pytest.mark.parametrize("kind", ["ntfy", "bark", "signal"])
async def test_legacy_credential_base_url_is_opaque_and_kept(
    kind, async_client, notification_provider_factory, db_session, monkeypatch
):
    config = {**CASES[kind], "server": "https://operator:CANARY_URL@local.example/base?key=CANARY_QUERY"}
    provider = await notification_provider_factory(provider_type=kind, config=config)
    response = await async_client.get(f"/api/v1/notifications/{provider.id}")
    assert "CANARY" not in response.text and "server" not in response.json()["config"]
    assert response.json()["secrets_configured"]["server"] is True
    saved = await async_client.patch(
        f"/api/v1/notifications/{provider.id}", json={"name": "Rename", "config": response.json()["config"]}
    )
    assert saved.status_code == 200, saved.text
    await db_session.refresh(provider)
    assert json.loads(provider.config)["server"] == config["server"]
    send = AsyncMock(return_value=(False, quote(config["server"], safe="")))
    monkeypatch.setattr(notifications.notification_service, "send_test_notification", send)
    tested = await async_client.post(
        f"/api/v1/notifications/{provider.id}/test-config",
        json={"provider_type": kind, "config": response.json()["config"]},
    )
    assert tested.status_code == 200 and "CANARY" not in tested.text
    assert send.call_args.args[1]["server"] == config["server"]


async def test_new_draft_cannot_resolve_saved_credentials_and_no_transaction_over_io(
    async_client, notification_provider_factory, monkeypatch
):
    provider = await notification_provider_factory(provider_type="ntfy", config=CASES["ntfy"])
    response = await async_client.post(
        "/api/v1/notifications/test-config", json={"provider_type": "ntfy", "provider_id": provider.id, "config": {}}
    )
    assert response.status_code == 403

    async def send(kind, config, db, **kwargs):
        assert not db.in_transaction()
        return True, "sent"

    monkeypatch.setattr(notifications.notification_service, "send_test_notification", send)
    tested = await async_client.post(
        f"/api/v1/notifications/{provider.id}/test-config",
        json={"provider_type": "ntfy", "config": {"server": CASES["ntfy"]["server"]}},
    )
    assert tested.status_code == 200, tested.text


async def test_anonymous_clear_requires_explicit_mode_and_disabled_required_secret(
    async_client, notification_provider_factory
):
    provider = await notification_provider_factory(provider_type="ntfy", config=CASES["ntfy"])
    door = f"/api/v1/notifications/{provider.id}"
    response = await async_client.patch(door, json={"secret_actions": {"auth_token": "clear"}})
    assert response.status_code == 422
    response = await async_client.patch(
        door,
        json={
            "config": {"server": CASES["ntfy"]["server"], "auth_mode": "anonymous"},
            "secret_actions": {"auth_token": "clear"},
        },
    )
    assert response.status_code == 200, response.text
    response = await async_client.patch(door, json={"secret_actions": {"topic": "clear", "auth_token": "clear"}})
    assert response.status_code == 422


async def test_provider_type_change_cannot_carry_other_provider_secret(
    async_client, notification_provider_factory, db_session
):
    provider = await notification_provider_factory(provider_type="ntfy", config=CASES["ntfy"])
    response = await async_client.patch(
        f"/api/v1/notifications/{provider.id}", json={"provider_type": "signal", "config": CASES["signal"]}
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(provider)
    assert "CANARY_NTFY" not in provider.config and "CANARY_TOPIC" not in provider.config
    assert json.loads(provider.config)["auth_header"] == CASES["signal"]["auth_header"]


async def test_historical_notification_log_redacts_known_configured_material(
    async_client, notification_provider_factory, db_session
):
    provider = await notification_provider_factory(provider_type="webhook", config=CASES["webhook"])
    db_session.add(
        NotificationLog(
            provider_id=provider.id,
            event_type="test",
            title="peer CANARY_AUTH",
            message=quote(CASES["webhook"]["webhook_url"], safe=""),
            success=False,
            error_message="Authorization rejected CANARY_AUTH",
        )
    )
    await db_session.commit()
    response = await async_client.get("/api/v1/notifications/logs")
    assert response.status_code == 200, response.text
    assert "CANARY" not in response.text


async def test_test_all_does_not_carry_status_writer_into_next_sender(
    async_client, notification_provider_factory, monkeypatch
):
    await notification_provider_factory(provider_type="ntfy", config=CASES["ntfy"])
    await notification_provider_factory(provider_type="bark", config=CASES["bark"])
    calls = []

    async def send(kind, config, db, **kwargs):
        assert not db.in_transaction()
        calls.append(kind)
        return False, "peer rejected"

    monkeypatch.setattr(notifications.notification_service, "send_test_notification", send)
    response = await async_client.post("/api/v1/notifications/test-all")
    assert response.status_code == 200, response.text
    assert calls == ["ntfy", "bark"]


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("callmebot", {"phone": " +15551234567 "}),
        ("ntfy", {"server": "https://NTFY.example:443/base/"}),
        ("bark", {"server": "https://BARK.example:443/base/"}),
        ("email", {"smtp_server": "SMTP.EXAMPLE", "smtp_port": 587, "security": "starttls"}),
        ("signal", {"server": "https://SIGNAL.example:443/v1/send/"}),
        ("webhook", {"webhook_url": "https://HOOKS.example:443/CANARY_WEBHOOK"}),
    ],
)
async def test_sender_normalized_noop_keeps_credentials(
    kind, changes, async_client, notification_provider_factory, monkeypatch
):
    provider = await notification_provider_factory(provider_type=kind, config=CASES[kind])
    safe, _, _ = provider_projection(kind, CASES[kind])
    config = {**safe, **changes}
    send = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(notifications.notification_service, "send_test_notification", send)
    response = await async_client.post(
        f"/api/v1/notifications/{provider.id}/test-config", json={"provider_type": kind, "config": config}
    )
    assert response.status_code == 200, response.text
    assert send.await_count == 1
    for key in SECRET_FIELDS[kind] - changes.keys():
        assert send.call_args.args[1][key] == CASES[kind][key]
