"""A Telegram chat belongs to the bot it wrote to (m180) — the routes' and the fan-out's half.

A manually added chat is registered under the provider named; unnamed is
answerable only when the install has exactly one Telegram provider, and with
none there is no chat to add. The same chat id under two bots is two chats. A
provider's fan-out reaches its own chats and no other's, and its test message
goes through its own bot. Deleting a telegram provider takes its chats with
it — in code, because SQLite never gets ``PRAGMA foreign_keys``.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services.notification_service import NotificationService

pytestmark = pytest.mark.integration


@pytest.fixture
def restart_bot():
    """The provider routes' restart, silenced — no network here."""
    with patch("backend.app.services.telegram_bot.restart_telegram_bot", new_callable=AsyncMock) as mock:
        yield mock


async def _bot(notification_provider_factory, token: str, **kwargs):
    kwargs.setdefault("provider_type", "telegram")
    kwargs.setdefault("config", {"bot_token": token})
    return await notification_provider_factory(**kwargs)


async def _add_chat(async_client: AsyncClient, chat_id: int, **fields):
    payload = {"chat_id": chat_id, "is_active": True, "notify_events": ["print_complete"], **fields}
    return await async_client.post("/api/v1/telegram/chats", json=payload)


class TestChatBinding:
    @pytest.mark.asyncio
    async def test_a_chat_added_from_a_providers_card_belongs_to_that_provider(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        older = await _bot(notification_provider_factory, "111:AAolder")
        younger = await _bot(notification_provider_factory, "222:AAyounger")

        response = await _add_chat(async_client, 100, provider_id=younger.id)

        assert response.status_code == 201
        assert response.json()["provider_id"] == younger.id
        listed = (await async_client.get("/api/v1/telegram/chats")).json()
        assert {c["chat_id"]: c["provider_id"] for c in listed} == {100: younger.id}
        assert older.id != younger.id

    @pytest.mark.asyncio
    async def test_with_several_bots_the_provider_must_be_named(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """Guessing would file the chat under a bot it never wrote to, and it would never be messaged."""
        await _bot(notification_provider_factory, "111:AAfirst")
        await _bot(notification_provider_factory, "222:AAsecond")

        response = await _add_chat(async_client, 101)

        assert response.status_code == 400
        assert "Name the Telegram provider" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_with_exactly_one_bot_it_takes_the_chat_unasked(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """One Telegram provider, switched off or not, is the only bot a chat could mean."""
        only = await _bot(notification_provider_factory, "111:AAoff", enabled=False)

        response = await _add_chat(async_client, 102)

        assert response.status_code == 201
        assert response.json()["provider_id"] == only.id

    @pytest.mark.asyncio
    async def test_with_no_bot_at_all_there_is_nothing_to_bind_to(
        self, async_client: AsyncClient, notification_provider_factory
    ):
        await notification_provider_factory(provider_type="ntfy")

        response = await _add_chat(async_client, 103)

        assert response.status_code == 409
        assert "Telegram provider" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_provider_that_is_not_a_bot_is_refused(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        ntfy = await notification_provider_factory(provider_type="ntfy")

        assert (await _add_chat(async_client, 104, provider_id=ntfy.id)).status_code == 400
        assert (await _add_chat(async_client, 105, provider_id=ntfy.id + 1000)).status_code == 404

    @pytest.mark.asyncio
    async def test_the_same_chat_id_under_two_bots_is_two_chats(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """Telegram's private chat id is the person's user id — the same number in every bot."""
        a = await _bot(notification_provider_factory, "111:AAa")
        b = await _bot(notification_provider_factory, "222:AAb")

        assert (await _add_chat(async_client, 600, provider_id=a.id)).status_code == 201
        assert (await _add_chat(async_client, 600, provider_id=b.id)).status_code == 201
        again = await _add_chat(async_client, 600, provider_id=a.id)
        assert again.status_code == 400, "the same person twice in the SAME bot is one chat"

        listed = (await async_client.get("/api/v1/telegram/chats")).json()
        assert sorted(c["provider_id"] for c in listed if c["chat_id"] == 600) == sorted([a.id, b.id])

    @pytest.mark.asyncio
    async def test_the_test_button_sends_through_the_chats_own_bot(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        a = await _bot(notification_provider_factory, "111:AAa")
        b = await _bot(notification_provider_factory, "222:AAb")
        created = await _add_chat(async_client, 700, provider_id=b.id)
        assert created.status_code == 201
        chat_db_id = created.json()["id"]

        with patch("backend.app.services.telegram_bot.send_message", AsyncMock(return_value=True)) as send:
            response = await async_client.post(f"/api/v1/telegram/chats/{chat_db_id}/test")

        assert response.status_code == 200
        assert send.await_args.args[0] == b.id, "the chat's own bot, not the first telegram provider"
        assert send.await_args.args[1] == 700
        assert a.id != b.id


class TestChatsFollowTheirBot:
    @pytest.mark.asyncio
    async def test_a_providers_fan_out_reaches_its_own_chats_only(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot, monkeypatch
    ):
        a = await _bot(notification_provider_factory, "111:AAa")
        b = await _bot(notification_provider_factory, "222:AAb")
        for chat_id, owner in ((201, a), (202, a), (203, b)):
            assert (await _add_chat(async_client, chat_id, provider_id=owner.id)).status_code == 201

        sent: list[str] = []

        async def fake_send(self, config, message, chat_id="", **kwargs):
            sent.append(chat_id)
            return True, "ok"

        monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
        svc = NotificationService()
        await svc._send_telegram_to_chats(
            {"bot_token": "111:AAa"}, "msg", event_type="print_complete", provider_id=a.id
        )
        assert sorted(sent) == ["201", "202"]

        sent.clear()
        await svc._send_telegram_to_chats(
            {"bot_token": "222:AAb"}, "msg", event_type="print_complete", provider_id=b.id
        )
        assert sent == ["203"]

    @pytest.mark.asyncio
    async def test_the_digest_fan_out_reaches_its_own_chats_only(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot, monkeypatch
    ):
        a = await _bot(notification_provider_factory, "111:AAa")
        b = await _bot(notification_provider_factory, "222:AAb")
        assert (await _add_chat(async_client, 301, provider_id=a.id, daily_digest=True)).status_code == 201
        assert (await _add_chat(async_client, 302, provider_id=b.id, daily_digest=True)).status_code == 201

        sent: list[str] = []

        async def fake_send(self, config, message, chat_id="", **kwargs):
            sent.append(chat_id)
            return True, "ok"

        monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
        await NotificationService()._send_telegram_digest_to_chats({"bot_token": "222:AAb"}, "t", "b", provider_id=b.id)
        assert sent == ["302"]

    @pytest.mark.asyncio
    async def test_deleting_a_bot_takes_its_chats_and_leaves_the_others(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        a = await _bot(notification_provider_factory, "111:AAa")
        b = await _bot(notification_provider_factory, "222:AAb")
        assert (await _add_chat(async_client, 401, provider_id=a.id)).status_code == 201
        assert (await _add_chat(async_client, 402, provider_id=b.id)).status_code == 201

        assert (await async_client.delete(f"/api/v1/notifications/{a.id}")).status_code == 200

        listed = (await async_client.get("/api/v1/telegram/chats")).json()
        assert {c["chat_id"]: c["provider_id"] for c in listed} == {402: b.id}

    @pytest.mark.asyncio
    async def test_deleting_a_provider_that_is_not_a_bot_touches_no_chat(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        a = await _bot(notification_provider_factory, "111:AAa")
        ntfy = await notification_provider_factory(provider_type="ntfy")
        assert (await _add_chat(async_client, 501, provider_id=a.id)).status_code == 201

        assert (await async_client.delete(f"/api/v1/notifications/{ntfy.id}")).status_code == 200

        listed = (await async_client.get("/api/v1/telegram/chats")).json()
        assert [c["chat_id"] for c in listed] == [501]
