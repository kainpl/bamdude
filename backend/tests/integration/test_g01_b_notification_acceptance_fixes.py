"""Opus BA5/6/7/11/12/13 notification regressions at real HTTP/runtime doors."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from backend.app.api.routes import notifications
from backend.app.models.notification import NotificationLog
from backend.app.services import telegram_bot
from backend.app.services.notification_service import NotificationService
from backend.tests.integration.test_g01_b_notification_secrets import CASES, no_bot_network  # noqa: F401


@pytest.mark.parametrize("kind,field", [("ntfy", "auth_token"), ("webhook", "auth_header"), ("signal", "auth_header")])
async def test_whitespace_auth_refused_by_save_and_draft_without_persistence_or_outbound(
    kind, field, async_client, notification_provider_factory, db_session, monkeypatch
):
    original = CASES[kind]
    provider = await notification_provider_factory(provider_type=kind, config=original)
    send = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(notifications.notification_service, "send_test_notification", send)
    config = {**original, field: " \t "}
    for suffix, body in (
        ("", {"config": config}),
        ("/test-config", {"provider_type": kind, "config": config}),
    ):
        request = async_client.patch if not suffix else async_client.post
        result = await request(f"/api/v1/notifications/{provider.id}{suffix}", json=body)
        assert result.status_code == 422, result.text
    send.assert_not_awaited()
    await db_session.refresh(provider)
    assert json.loads(provider.config) == original


@pytest.mark.parametrize("running_token", ["111:CANARY_OLD", "000:CANARY_STALE"])
async def test_replaced_telegram_draft_uses_new_token_and_same_token_reuses_owned_bot(
    running_token, async_client, notification_provider_factory, db_session, monkeypatch
):
    provider = await notification_provider_factory(provider_type="telegram", config={"bot_token": "111:CANARY_OLD"})
    sent = []
    monkeypatch.setattr(telegram_bot, "get_bot", lambda pid: SimpleNamespace(token=running_token))

    async def send(pid, chat_id, text, reply_markup=None):
        sent.append((pid, chat_id))
        return True

    monkeypatch.setattr(telegram_bot, "send_message", send)

    class Chats:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args, **kwargs):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(chat_id=42)]))

    monkeypatch.setattr("backend.app.core.database.async_session", lambda: Chats())
    monkeypatch.setattr(NotificationService, "_build_telegram_actions", AsyncMock(return_value=None))
    client = AsyncMock()

    async def reject_requested_token(url, **kwargs):
        token = url.split("/bot", 1)[1].split("/", 1)[0]
        return httpx.Response(401, text="rejected " + token)

    client.post.side_effect = reject_requested_token
    monkeypatch.setattr(notifications.notification_service, "_get_client", AsyncMock(return_value=client))
    door = f"/api/v1/notifications/{provider.id}/test-config"
    result = await async_client.post(
        door,
        json={
            "provider_type": "telegram",
            "config": {"bot_token": "222:CANARY_NEW_INVALID", "chat_id": "42"},
            "secret_actions": {"bot_token": "replace"},
        },
    )
    assert result.status_code == 200 and not result.json()["success"] and "CANARY" not in result.text
    assert not sent
    assert client.post.call_args.args[0] == "https://api.telegram.org/bot222:CANARY_NEW_INVALID/sendMessage"
    result = await async_client.post(
        door,
        json={
            "provider_type": "telegram",
            "config": {},
            "secret_actions": {"bot_token": "keep"},
        },
    )
    assert result.status_code == 200 and "CANARY" not in result.text
    if running_token == "111:CANARY_OLD":
        assert result.json()["success"] and sent == [(provider.id, "42")]
        assert client.post.await_count == 1
    else:
        # A committed rotation can be visible before the poller restarts.
        # The stale running bot cannot prove delivery with the frozen token.
        assert not result.json()["success"] and not sent
        assert client.post.await_count == 2
        assert client.post.call_args.args[0] == "https://api.telegram.org/bot111:CANARY_OLD/sendMessage"
    await db_session.refresh(provider)
    assert json.loads(provider.config) == {"bot_token": "111:CANARY_OLD"}


async def test_ordinary_diagnostics_survive_short_webhook_path(async_client, notification_provider_factory, db_session):
    provider = await notification_provider_factory(
        provider_type="webhook",
        config={"webhook_url": "https://hooks.example/print/a"},
        last_error="HTTP 500: a printer failed",
    )
    prose = "Your print finished on a printer named Alpha"
    db_session.add(
        NotificationLog(
            provider_id=provider.id,
            event_type="print_complete",
            title="Print finished",
            message=prose,
            success=True,
        )
    )
    await db_session.commit()
    assert (await async_client.get("/api/v1/notifications/logs")).json()[0]["message"] == prose
    assert (await async_client.get(f"/api/v1/notifications/{provider.id}")).json()[
        "last_error"
    ] == "HTTP 500: a printer failed"


@pytest.mark.parametrize("outer_exception", [False, True])
async def test_runtime_errors_remain_safe_after_all_original_credentials_rotate(
    outer_exception, async_client, notification_provider_factory, db_session, monkeypatch, caplog
):
    config = {
        "webhook_url": "https://hooks.example/print/CANARY_OLD_CAPABILITY?key=CANARY_OLD_QUERY",
        "auth_header": "Bearer CANARY_OLD_HEADER",
    }
    provider = await notification_provider_factory(provider_type="webhook", config=config)
    service = NotificationService()
    echoed = "CANARY_OLD_CAPABILITY CANARY_OLD_QUERY CANARY_OLD_HEADER"
    if outer_exception:
        monkeypatch.setattr(service, "_send_to_provider", AsyncMock(side_effect=RuntimeError(echoed)))
    else:
        client = AsyncMock()
        client.post.return_value = httpx.Response(400, text=echoed)
        monkeypatch.setattr(service, "_get_client", AsyncMock(return_value=client))
    await service._send_to_providers([provider], "Print", "A printer finished", db_session, event_type="print_complete")
    await db_session.commit()
    await db_session.refresh(provider)
    log = await db_session.scalar(select(NotificationLog).where(NotificationLog.provider_id == provider.id))
    assert "CANARY" not in provider.last_error and "CANARY" not in log.error_message
    assert "CANARY" not in caplog.text
    result = await async_client.patch(
        f"/api/v1/notifications/{provider.id}",
        json={
            "config": {"webhook_url": "https://new.example/CANARY_NEW_URL", "auth_header": "Bearer CANARY_NEW_HEADER"},
            "secret_actions": {"webhook_url": "replace", "auth_header": "replace"},
        },
    )
    assert result.status_code == 200, result.text
    for door in (f"/api/v1/notifications/{provider.id}", "/api/v1/notifications/logs"):
        assert "CANARY_OLD" not in (await async_client.get(door)).text


@pytest.mark.parametrize("server", ["", None])
async def test_nonworking_ntfy_server_cannot_become_public_with_retained_credentials(
    server, async_client, notification_provider_factory, db_session, monkeypatch
):
    original = {**CASES["ntfy"], "server": server}
    provider = await notification_provider_factory(provider_type="ntfy", config=original)
    send = AsyncMock(return_value=(True, "sent"))
    monkeypatch.setattr(notifications.notification_service, "send_test_notification", send)
    for suffix, body in (
        ("", {"config": {"server": "https://ntfy.sh"}}),
        ("/test-config", {"provider_type": "ntfy", "config": {"server": "https://ntfy.sh"}}),
    ):
        request = async_client.patch if not suffix else async_client.post
        response = await request(f"/api/v1/notifications/{provider.id}{suffix}", json=body)
        assert response.status_code == 400, response.text
    send.assert_not_awaited()
    await db_session.refresh(provider)
    assert json.loads(provider.config) == original
