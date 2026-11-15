"""News that belongs to no printer does not reach a recipient bound to one.

Adapted from upstream e05a91744. A provider or a Telegram chat limited to
printer 7 asked for that printer's news. Two kinds of printer-less news still
reached it:

- the AutoQueue waiting digest («Any X1C: …» obstacles of the whole farm) went
  to every provider and every chat, scoped or not;
- room / location sensor alerts were already kept from scoped PROVIDERS, but a
  scoped Telegram CHAT still got them — the two rules disagreed.

Genuinely farm-wide events (the queue completed, stock) still reach everybody,
and an alert of a sensor that stands ON a printer still follows the printer.

⚠️ No provider-level switch is involved for Telegram: the provider row carries
no scope, and each chat decides for itself (per-chat authority, m045 / m157).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.models.notification import NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.services import notification_inbox
from backend.app.services.notification_service import NotificationService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

SCOPED_CHAT = 700
FREE_CHAT = 100


async def _ntfy(db_session, name: str, events: list[str], printer_ids: list[int] | None) -> NotificationProvider:
    row = NotificationProvider(
        name=name,
        provider_type="ntfy",
        enabled=True,
        config=json.dumps({"topic": name, "server_url": "https://ntfy.sh"}),
        subscribed_events=events,
        printer_ids=printer_ids,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _bot_with_chats(db_session, events: list[str]) -> None:
    """One bot, two chats subscribed to ``events``: one limited to printer 7, one unlimited."""
    bot = NotificationProvider(
        name="bot", provider_type="telegram", enabled=True, config=json.dumps({"bot_token": "123:ABC"})
    )
    db_session.add(bot)
    await db_session.commit()
    await db_session.refresh(bot)
    scoped = TelegramChat(chat_id=SCOPED_CHAT, provider_id=bot.id, is_active=True, notify_events=events)
    scoped.printer_ids = [7]
    free = TelegramChat(chat_id=FREE_CHAT, provider_id=bot.id, is_active=True, notify_events=events)
    db_session.add_all([scoped, free])
    await db_session.commit()


@pytest.fixture
def deliveries(monkeypatch):
    """Who was actually sent to: ntfy topics, Telegram chat ids, inbox events."""
    sent = SimpleNamespace(ntfy=[], chats=[], inbox=[])

    async def fake_ntfy(self, config, title, message, **kwargs):
        sent.ntfy.append(config["topic"])
        return True, "ok"

    async def fake_telegram(self, config, message, chat_id="", **kwargs):
        sent.chats.append(int(chat_id))
        return True, "ok"

    async def fake_inbox(db, *, event_type, **kwargs):
        sent.inbox.append(event_type)

    monkeypatch.setattr(NotificationService, "_send_ntfy", fake_ntfy)
    monkeypatch.setattr(NotificationService, "_send_telegram", fake_telegram)
    monkeypatch.setattr(notification_inbox, "deliver", AsyncMock(side_effect=fake_inbox))
    return sent


class TestTheAutoQueueWaitingDigest:
    async def test_goes_to_unscoped_providers_only(self, async_client, db_session, deliveries):
        await _ntfy(db_session, "bound", ["on_queue_job_waiting"], [7])
        await _ntfy(db_session, "free", ["on_queue_job_waiting"], None)

        await NotificationService().on_queue_job_waiting("2 jobs", "X1C", "No matching filament", db_session)

        assert deliveries.ntfy == ["free"]

    async def test_goes_to_unscoped_chats_only(self, async_client, db_session, deliveries):
        await _bot_with_chats(db_session, ["queue_job_waiting"])

        await NotificationService().on_queue_job_waiting("2 jobs", "X1C", "No matching filament", db_session)

        assert deliveries.chats == [FREE_CHAT]

    async def test_still_reaches_the_inbox(self, async_client, db_session, deliveries):
        """The inbox is a channel with no printer scope; a farm with only scoped
        recipients still sees the digest in the app."""
        await _ntfy(db_session, "bound", ["on_queue_job_waiting"], [7])

        await NotificationService().on_queue_job_waiting("2 jobs", "X1C", "No matching filament", db_session)

        assert deliveries.inbox == ["queue_job_waiting"]
        assert deliveries.ntfy == []


class TestRoomSensorAlerts:
    @staticmethod
    def _alert(printer_id: int | None):
        return SimpleNamespace(
            template="sensor_above_max",
            printer_id=printer_id,
            variables={"location": "Storage", "sensor": "S1", "quantity": "humidity", "value": "70", "limit": "60"},
        )

    async def test_a_room_alert_skips_a_printer_scoped_chat(self, async_client, db_session, deliveries):
        """What a scoped provider already did not get, a scoped chat no longer gets either."""
        await _bot_with_chats(db_session, ["sensor_above_max"])

        await NotificationService().on_sensor_alert(self._alert(None), db_session)

        assert deliveries.chats == [FREE_CHAT]

    async def test_an_alert_on_the_chats_own_printer_still_reaches_it(self, async_client, db_session, deliveries):
        await _bot_with_chats(db_session, ["sensor_above_max"])

        await NotificationService().on_sensor_alert(self._alert(7), db_session)

        assert sorted(deliveries.chats) == [FREE_CHAT, SCOPED_CHAT]

    async def test_an_alert_on_another_printer_does_not(self, async_client, db_session, deliveries):
        await _bot_with_chats(db_session, ["sensor_above_max"])

        await NotificationService().on_sensor_alert(self._alert(8), db_session)

        assert deliveries.chats == [FREE_CHAT]

    async def test_a_location_ha_sensor_alert_skips_a_printer_scoped_chat(self, async_client, db_session, deliveries):
        await _bot_with_chats(db_session, ["location_ha_sensor_alert"])

        await NotificationService().on_location_ha_sensor_alert("Storage", "Door", "open", db_session)

        assert deliveries.chats == [FREE_CHAT]


class TestFarmWideNewsStillReachesEverybody:
    async def test_the_queue_completed_reaches_scoped_recipients_too(self, async_client, db_session, deliveries):
        await _ntfy(db_session, "bound", ["on_queue_completed"], [7])
        await _bot_with_chats(db_session, ["queue_completed"])

        await NotificationService().on_queue_completed(3, db_session)

        assert deliveries.ntfy == ["bound"]
        assert sorted(deliveries.chats) == [FREE_CHAT, SCOPED_CHAT]
