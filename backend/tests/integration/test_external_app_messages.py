"""Messages from external apps (spec vault 60-specs/external-app-messages-spec, owner 2026-11-21).

Another program of the farm -- Home Assistant, a script -- says something to the farm's people through
BamDude's own channels: one catalog event, ``external_app_message``, that every provider and Telegram chat
turns on for itself, shown to everybody in the in-app inbox, with the sender's level and the sender's name.
Only an API key the owner allowed to (``can_send_notifications``) may send, and never so as to pass for
BamDude: the sender is added by the code, not by an editable template.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.core.auth import generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.notification import NotificationProvider
from backend.app.models.notification_template import NotificationTemplate
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services import external_messages
from backend.tests.auth_helpers import bind_test_key

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

URL = "/api/v1/notifications/external-message"
_DATA = Path(__file__).resolve().parents[2] / "app" / "data"


@pytest.fixture(autouse=True)
def _fresh_window():
    external_messages.reset_rate_limit()
    yield
    external_messages.reset_rate_limit()


async def _user(db_session, username: str, permissions: list[str], *, is_active: bool = True) -> User:
    group = Group(name=f"grp-{username}", description="test", permissions=permissions)
    db_session.add(group)
    await db_session.flush()
    user = User(username=username, email=f"{username}@example.com", password_hash="x", role="user", is_active=is_active)
    user.groups.append(group)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _key(db_session, owner: User, *, name: str = "Home Assistant", **flags) -> tuple[str, APIKey]:
    raw, key_hash, key_prefix = generate_api_key()
    api_key = APIKey(name=name, key_hash=key_hash, key_prefix=key_prefix, enabled=True, user_id=owner.id, **flags)
    await bind_test_key(db_session, api_key, owner)
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return raw, api_key


async def _sender(db_session, *, flag: bool = True, permissions: list[str] | None = None) -> tuple[str, APIKey, User]:
    owner = await _user(
        db_session,
        "kain",
        permissions if permissions is not None else [Permission.NOTIFICATIONS_UPDATE.value, "notifications:inbox"],
    )
    raw, key = await _key(db_session, owner, can_send_notifications=flag)
    return raw, key, owner


async def _seed_template(db_session, lang: str = "en") -> None:
    shipped = json.loads((_DATA / f"notification_templates_{lang}.json").read_text(encoding="utf-8"))
    row = shipped["external_app_message"]
    db_session.add(
        NotificationTemplate(
            event_type="external_app_message",
            name=row["name"],
            title_template=row["title_template"],
            body_template=row["body_template"],
            is_default=True,
        )
    )
    await db_session.commit()


def _as(raw: str) -> dict:
    return {"X-API-Key": raw}


async def _inbox(db_session) -> list[UserNotification]:
    return list(
        (
            await db_session.execute(
                select(UserNotification)
                .where(UserNotification.event_type == "external_app_message")
                .order_by(UserNotification.user_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


# --- who may send ------------------------------------------------------------------------------------------


async def test_a_key_without_the_flag_is_refused(async_client, db_session):
    raw, _, _ = await _sender(db_session, flag=False)

    response = await async_client.post(URL, json={"title": "Power", "message": "gone"}, headers=_as(raw))

    assert response.status_code == 403, response.text
    assert await _inbox(db_session) == []


async def test_a_browser_session_is_refused(async_client, db_session):
    # The test client signs in as the administrator: the door is for programs, not for people.
    response = await async_client.post(URL, json={"title": "Power", "message": "gone"})

    assert response.status_code == 403, response.text
    assert await _inbox(db_session) == []


async def test_a_deactivated_owner_is_refused(async_client, db_session):
    raw, _, owner = await _sender(db_session)
    owner.is_active = False
    await db_session.commit()

    response = await async_client.post(URL, json={"title": "Power", "message": "gone"}, headers=_as(raw))

    assert response.status_code in (401, 403), response.text
    assert await _inbox(db_session) == []


async def test_an_owner_without_notifications_update_is_refused(async_client, db_session):
    raw, _, _ = await _sender(db_session, permissions=["notifications:inbox"])

    response = await async_client.post(URL, json={"title": "Power", "message": "gone"}, headers=_as(raw))

    assert response.status_code == 403, response.text
    assert await _inbox(db_session) == []


async def test_the_flag_is_saved_and_read_back(async_client):
    created = await async_client.post("/api/v1/api-keys/", json={"name": "HA", "can_send_notifications": True})
    assert created.status_code == 200, created.text
    assert created.json()["can_send_notifications"] is True
    key_id = created.json()["id"]

    updated = await async_client.patch(f"/api/v1/api-keys/{key_id}", json={"can_send_notifications": False})
    assert updated.status_code == 200, updated.text
    assert updated.json()["can_send_notifications"] is False

    assert (await async_client.post("/api/v1/api-keys/", json={"name": "old"})).json()[
        "can_send_notifications"
    ] is False


# --- what everybody sees ---------------------------------------------------------------------------------


async def test_every_user_sees_it_in_the_inbox_with_its_level_and_sender(async_client, db_session):
    await _seed_template(db_session)
    raw, key, owner = await _sender(db_session)

    response = await async_client.post(
        URL,
        json={"title": "Power lost", "message": "The farm is on battery.", "level": "error"},
        headers=_as(raw),
    )

    assert response.status_code == 200, response.text
    rows = await _inbox(db_session)
    assert rows, "the inbox shows external messages to everybody by default"
    assert response.json()["inbox_recipients"] == len(rows)
    for row in rows:
        assert row.severity == "error", "the sender's level, not the catalog's"
        assert row.title == "🚨 Power lost"
        assert row.message.startswith("The farm is on battery.")
        assert "Home Assistant" not in row.message, "the inbox shows the sender on its own line, not in the text"
        assert row.extra_data["sender"] == {
            "api_key_id": key.id,
            "key_name": "Home Assistant",
            "owner_id": owner.id,
            "owner_name": "kain",
        }


async def test_the_level_defaults_to_info(async_client, db_session):
    await _seed_template(db_session)
    raw, _, _ = await _sender(db_session)

    assert (
        await async_client.post(URL, json={"title": "Batch", "message": "done"}, headers=_as(raw))
    ).status_code == 200

    rows = await _inbox(db_session)
    assert rows and {row.severity for row in rows} == {"info"}
    assert rows[0].title == "ℹ️ Batch"


def _provider(name: str, events: list[str], **fields) -> NotificationProvider:
    return NotificationProvider(
        name=name,
        provider_type="ntfy",
        enabled=True,
        config=json.dumps({"topic": name}),
        subscribed_events=events,
        quiet_hours_enabled=False,
        daily_digest_enabled=False,
        **fields,
    )


async def test_a_subscribed_provider_gets_it_with_the_sender_line_and_others_do_not(async_client, db_session):
    from backend.app.services.notification_service import notification_service

    await _seed_template(db_session)
    db_session.add_all(
        [
            _provider("subscribed", ["on_external_app_message"]),
            _provider("scoped", ["on_external_app_message"], printer_ids=[999]),
            _provider("other", ["on_print_complete"]),
        ]
    )
    await db_session.commit()
    raw, _, _ = await _sender(db_session)

    heard: list[tuple[str, str, str]] = []

    async def capture(provider, title, message, db=None, **kwargs):
        heard.append((provider.name, title, message))
        return True, ""

    notification_service._template_cache.clear()
    with (
        patch.object(notification_service, "_send_to_provider", capture),
        patch.object(notification_service, "_update_provider_status", AsyncMock()),
        patch.object(notification_service, "_log_notification", AsyncMock()),
    ):
        response = await async_client.post(
            URL,
            json={"title": "Power lost", "message": "On battery.", "level": "warning", "link": "https://ha.local/x"},
            headers=_as(raw),
        )
    notification_service._template_cache.clear()

    assert response.status_code == 200, response.text
    # Farm-wide news reaches a printer-scoped provider too, as the other farm-wide events do.
    assert sorted(name for name, _, _ in heard) == ["scoped", "subscribed"]
    assert response.json()["channels"] == 2
    for _, title, message in heard:
        assert title == "⚠️ Power lost"
        assert "https://ha.local/x" in message
        assert message.rstrip().endswith("— Home Assistant · kain"), message


async def test_the_sender_line_survives_an_edited_template(async_client, db_session):
    from backend.app.services.notification_service import notification_service

    db_session.add(
        NotificationTemplate(
            event_type="external_app_message",
            name="edited",
            title_template="{title}",
            body_template="{message}",
            is_default=False,
        )
    )
    db_session.add(_provider("subscribed", ["on_external_app_message"]))
    await db_session.commit()
    raw, _, _ = await _sender(db_session)
    heard: list[str] = []

    async def capture(provider, title, message, db=None, **kwargs):
        heard.append(message)
        return True, ""

    notification_service._template_cache.clear()
    with (
        patch.object(notification_service, "_send_to_provider", capture),
        patch.object(notification_service, "_update_provider_status", AsyncMock()),
        patch.object(notification_service, "_log_notification", AsyncMock()),
    ):
        await async_client.post(URL, json={"title": "Print failed", "message": "X1C"}, headers=_as(raw))
    notification_service._template_cache.clear()

    assert heard and heard[0].rstrip().endswith("— Home Assistant · kain"), "no template can make it pass for BamDude"


def test_a_telegram_chat_decides_for_itself():
    from backend.app.models.telegram_chat import ALL_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS

    assert "external_app_message" in ALL_NOTIFY_EVENTS
    assert "external_app_message" not in DEFAULT_NOTIFY_EVENTS, "no chat starts receiving it unasked"


# --- what may be sent ------------------------------------------------------------------------------------


async def test_the_twenty_first_message_in_a_minute_is_refused(async_client, db_session):
    raw, _, _ = await _sender(db_session)
    other_owner = await _user(db_session, "other", [Permission.NOTIFICATIONS_UPDATE.value])
    other_raw, _ = await _key(db_session, other_owner, name="Script", can_send_notifications=True)

    codes = [
        (await async_client.post(URL, json={"title": f"n{i}", "message": "m"}, headers=_as(raw))).status_code
        for i in range(21)
    ]
    assert codes[:20] == [200] * 20
    refused = await async_client.post(URL, json={"title": "n", "message": "m"}, headers=_as(raw))
    assert codes[20] == 429 and refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1

    other = await async_client.post(URL, json={"title": "n", "message": "m"}, headers=_as(other_raw))
    assert other.status_code == 200, "the limit is per key"


async def test_text_is_plain(async_client, db_session):
    await _seed_template(db_session)
    raw, _, _ = await _sender(db_session)

    response = await async_client.post(
        URL, json={"title": "*Bold*\x07", "message": "a *b* c\x00\nnext\tline"}, headers=_as(raw)
    )

    assert response.status_code == 200, response.text
    row = (await _inbox(db_session))[0]
    assert row.title == "ℹ️ ∗Bold∗"
    assert row.message.startswith("a ∗b∗ c\nnext\tline")


@pytest.mark.parametrize(
    "body",
    [
        {"title": "t", "message": "m", "link": "ftp://example.com/x"},
        {"title": "t", "message": "m", "link": "javascript:alert(1)"},
        {"title": "t" * 121, "message": "m"},
        {"title": "t", "message": "m" * 2001},
        {"title": "", "message": "m"},
        {"title": "t", "message": ""},
        {"title": "t", "message": "m", "level": "fatal"},
        {"title": "\x07", "message": "m"},
    ],
    ids=["ftp", "javascript", "long-title", "long-message", "empty-title", "empty-message", "level", "only-control"],
)
async def test_a_malformed_message_is_refused(async_client, db_session, body):
    raw, _, _ = await _sender(db_session)

    response = await async_client.post(URL, json=body, headers=_as(raw))

    assert response.status_code == 422, response.text
    assert await _inbox(db_session) == []
