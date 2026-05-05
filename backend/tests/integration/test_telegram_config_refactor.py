"""Integration tests for the Telegram provider/chat config refactor (m045).

Covers the dispatch-routing changes:

* Per-event filter for telegram lives on ``TelegramChat.notify_events``;
  provider-level ``on_*`` is bypassed (telegram-row only).
* Non-telegram providers keep the legacy provider-level event gate.
* Daily digest fans out only to chats with ``daily_digest=True`` (was
  silently broken before — used ``should_notify("unknown")``).
* Digest queue write is skipped when no telegram chat opted in.
* Per-chat quiet hours block events even when provider says go.

Tests deliberately exercise ``NotificationService`` directly so they don't
have to mock httpx / aiogram. They monkey-patch ``_send_telegram`` to
record the chat IDs that *would* receive a message.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.notification import NotificationDigestQueue, NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.services.notification_service import NotificationService

pytestmark = pytest.mark.asyncio


async def _telegram_provider(
    db_session: AsyncSession,
    *,
    enabled: bool = True,
    daily_digest_enabled: bool = False,
    daily_digest_time: str | None = None,
    on_print_complete: bool = True,
) -> NotificationProvider:
    p = NotificationProvider(
        name="Telegram Test",
        provider_type="telegram",
        enabled=enabled,
        config=json.dumps({"bot_token": "test-token"}),
        on_print_start=True,
        on_print_complete=on_print_complete,
        on_print_failed=True,
        on_print_stopped=True,
        on_print_progress=True,
        on_print_missing_spool_assignment=True,
        on_printer_offline=True,
        on_printer_error=True,
        on_filament_low=True,
        on_maintenance_due=True,
        on_ams_humidity_high=True,
        on_ams_temperature_high=True,
        on_bed_cooled=True,
        quiet_hours_enabled=False,
        daily_digest_enabled=daily_digest_enabled,
        daily_digest_time=daily_digest_time,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def _email_provider(db_session: AsyncSession, *, on_print_complete: bool) -> NotificationProvider:
    p = NotificationProvider(
        name="Email Test",
        provider_type="email",
        enabled=True,
        config=json.dumps({"smtp_host": "x", "smtp_port": 25, "from_email": "x@y", "to_email": "y@z"}),
        on_print_complete=on_print_complete,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def _chat(
    db_session: AsyncSession,
    *,
    chat_id: int,
    is_active: bool = True,
    notify_events: list[str] | None = None,
    daily_digest: bool = False,
    quiet_hours_enabled: bool = False,
    quiet_hours_start: str | None = None,
    quiet_hours_end: str | None = None,
) -> TelegramChat:
    c = TelegramChat(
        chat_id=chat_id,
        is_active=is_active,
        notify_events=notify_events,
        daily_digest=daily_digest,
        quiet_hours_enabled=quiet_hours_enabled,
        quiet_hours_start=quiet_hours_start,
        quiet_hours_end=quiet_hours_end,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_telegram_provider_event_gate_bypassed(async_client, db_session, monkeypatch):
    """`_get_providers_for_event` must include telegram providers regardless
    of the legacy ``on_*`` flag — per-chat ``notify_events`` is the
    authority."""
    # Provider says NO to print_complete — but a chat opted into it.
    await _telegram_provider(db_session, on_print_complete=False)
    await _chat(db_session, chat_id=42, notify_events=["print_complete"])

    sent: list[str] = []

    async def fake_send(self, config, message, chat_id="", **kwargs):
        sent.append(chat_id)
        return True, "ok"

    monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
    svc = NotificationService()
    providers = await svc._get_providers_for_event(db_session, "on_print_complete")
    assert any(p.provider_type == "telegram" for p in providers), (
        "telegram provider must be returned even with on_print_complete=False"
    )

    # Now actually run the fan-out and confirm chat 42 received it.
    for p in providers:
        if p.provider_type == "telegram":
            cfg = json.loads(p.config)
            await svc._send_telegram_to_chats(cfg, "msg", event_type="print_complete")
    assert sent == ["42"]


async def test_non_telegram_provider_event_gate_still_applies(async_client, db_session):
    """Email / ntfy / pushover etc. keep the legacy provider-level gate."""
    await _email_provider(db_session, on_print_complete=False)
    svc = NotificationService()
    providers = await svc._get_providers_for_event(db_session, "on_print_complete")
    assert all(p.provider_type != "email" for p in providers), (
        "email provider with on_print_complete=False must NOT be returned"
    )


async def test_digest_routes_only_to_opted_in_chats(async_client, db_session, monkeypatch):
    """Telegram digest send must fan out only to chats with daily_digest=True."""
    provider = await _telegram_provider(db_session, daily_digest_enabled=True, daily_digest_time="08:00")
    await _chat(db_session, chat_id=1, daily_digest=True)
    await _chat(db_session, chat_id=2, daily_digest=False)
    await _chat(db_session, chat_id=3, daily_digest=True)

    # Seed digest queue with one entry so send_digest has something to ship.
    db_session.add(
        NotificationDigestQueue(
            provider_id=provider.id,
            event_type="print_complete",
            title="Done",
            message="Print done",
        )
    )
    await db_session.commit()

    sent: list[str] = []

    async def fake_send(self, config, message, chat_id="", **kwargs):
        sent.append(chat_id)
        return True, "ok"

    monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
    svc = NotificationService()
    await svc.send_digest(provider.id)

    assert sorted(sent) == ["1", "3"], "digest must reach only opted-in chats"

    # Queue is cleared after digest send.
    remaining = (
        (
            await db_session.execute(
                select(NotificationDigestQueue).where(NotificationDigestQueue.provider_id == provider.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_digest_with_no_opted_in_chats_clears_queue(async_client, db_session, monkeypatch):
    """When no telegram chats opted in, send_digest still clears the queue
    so the table doesn't grow forever — sends to zero chats successfully."""
    provider = await _telegram_provider(db_session, daily_digest_enabled=True, daily_digest_time="08:00")
    # One active chat exists, but not opted in.
    await _chat(db_session, chat_id=99, daily_digest=False)

    db_session.add(
        NotificationDigestQueue(
            provider_id=provider.id,
            event_type="print_complete",
            title="Done",
            message="Print done",
        )
    )
    await db_session.commit()

    sent: list[str] = []

    async def fake_send(self, config, message, chat_id="", **kwargs):
        sent.append(chat_id)
        return True, "ok"

    monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
    svc = NotificationService()
    await svc.send_digest(provider.id)

    assert sent == []
    remaining = (
        (
            await db_session.execute(
                select(NotificationDigestQueue).where(NotificationDigestQueue.provider_id == provider.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == [], "digest queue must clear even when no chats opted in"


async def test_chat_quiet_hours_blocks_event_even_when_provider_active(async_client, db_session, monkeypatch):
    """Per-chat quiet hours win over provider-side enable."""
    await _telegram_provider(db_session)
    # Quiet from 00:00 to 23:59 — covers any wall-clock time.
    await _chat(
        db_session,
        chat_id=77,
        notify_events=["print_complete"],
        quiet_hours_enabled=True,
        quiet_hours_start="00:00",
        quiet_hours_end="23:59",
    )

    sent: list[str] = []

    async def fake_send(self, config, message, chat_id="", **kwargs):
        sent.append(chat_id)
        return True, "ok"

    monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
    svc = NotificationService()
    cfg = {"bot_token": "test-token"}
    await svc._send_telegram_to_chats(cfg, "msg", event_type="print_complete")

    assert sent == [], "quiet-hours chat must not receive event"


async def test_skip_on_empty_telegram_digest_subscribers(async_client, db_session):
    """`_has_telegram_digest_subscribers` returns False when no chat opted
    in, True when any active chat has daily_digest=True."""
    svc = NotificationService()
    # No chats: False.
    assert await svc._has_telegram_digest_subscribers() is False

    await _chat(db_session, chat_id=1, daily_digest=False)
    assert await svc._has_telegram_digest_subscribers() is False

    await _chat(db_session, chat_id=2, daily_digest=True)
    assert await svc._has_telegram_digest_subscribers() is True

    # Inactive opted-in chat: still False (we only deliver to active).
    await _chat(db_session, chat_id=3, is_active=False, daily_digest=True)
    # The previous chat 2 is still active, so this stays True.
    assert await svc._has_telegram_digest_subscribers() is True
