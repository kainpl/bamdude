"""The daily digest is delivered or kept — never lost.

Two defects (upstream dad7fea37): a digest due inside the provider's quiet
hours was "skipped" by the ordinary sender, logged as sent and its queue
deleted; and a digest whose send failed lost its events all the same.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.app.models.notification import NotificationDigestQueue, NotificationProvider
from backend.app.services import notification_service as module
from backend.app.services.notification_service import NotificationService

pytestmark = pytest.mark.asyncio


async def ntfy(db, **fields) -> NotificationProvider:
    provider = NotificationProvider(
        name="ntfy",
        provider_type="ntfy",
        enabled=True,
        config=json.dumps({"server": "https://ntfy.invalid", "topic": "farm"}),
        subscribed_events=["on_print_complete"],
        daily_digest_enabled=True,
        daily_digest_time="08:00",
        **fields,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


async def queue(db, provider, title="Done", *, age=timedelta(minutes=5)):
    db.add(
        NotificationDigestQueue(
            provider_id=provider.id,
            event_type="print_complete",
            title=title,
            message="Print done",
            created_at=datetime.utcnow() - age,
        )
    )
    await db.commit()


async def queued(db, provider) -> list[str]:
    rows = await db.execute(
        select(NotificationDigestQueue.title)
        .where(NotificationDigestQueue.provider_id == provider.id)
        .order_by(NotificationDigestQueue.created_at)
    )
    return list(rows.scalars().all())


@pytest.fixture
def ntfy_sends(monkeypatch):
    calls: list[str] = []
    outcome = {"ok": True}

    async def fake(self, config, title, message, *args, **kwargs):
        calls.append(message)
        return (True, "ok") if outcome["ok"] else (False, "server down")

    monkeypatch.setattr(NotificationService, "_send_ntfy", fake)
    return calls, outcome


async def test_the_digest_goes_out_during_quiet_hours(async_client, db_session, ntfy_sends, monkeypatch):
    calls, _ = ntfy_sends
    provider = await ntfy(db_session, quiet_hours_enabled=True, quiet_hours_start="00:00", quiet_hours_end="23:59")
    monkeypatch.setattr(NotificationService, "_is_in_quiet_hours", lambda self, p: True)
    await queue(db_session, provider)

    await NotificationService().send_digest(provider.id)

    assert len(calls) == 1
    assert await queued(db_session, provider) == []


async def test_an_ordinary_notification_still_respects_quiet_hours(async_client, db_session, ntfy_sends, monkeypatch):
    calls, _ = ntfy_sends
    provider = await ntfy(db_session)
    monkeypatch.setattr(NotificationService, "_is_in_quiet_hours", lambda self, p: True)

    ok, detail = await NotificationService()._send_to_provider(provider, "t", "m", db_session)

    assert ok and "quiet" in detail.lower()
    assert calls == []


async def test_a_failed_digest_keeps_its_events_for_the_next(async_client, db_session, ntfy_sends):
    calls, outcome = ntfy_sends
    provider = await ntfy(db_session)
    await queue(db_session, provider, "First")
    outcome["ok"] = False

    await NotificationService().send_digest(provider.id)
    assert await queued(db_session, provider) == ["First"]

    await queue(db_session, provider, "Second", age=timedelta(minutes=1))
    outcome["ok"] = True
    await NotificationService().send_digest(provider.id)

    assert "First" in calls[-1] and "Second" in calls[-1]
    assert await queued(db_session, provider) == []


async def test_an_event_too_old_to_retry_is_dropped_unsent(async_client, db_session, ntfy_sends):
    calls, _ = ntfy_sends
    provider = await ntfy(db_session)
    await queue(db_session, provider, "Ancient", age=timedelta(hours=module.DIGEST_RETRY_HOURS + 1))
    await queue(db_session, provider, "Fresh")

    await NotificationService().send_digest(provider.id)

    assert "Fresh" in calls[-1] and "Ancient" not in calls[-1]
    assert await queued(db_session, provider) == []


async def test_only_stale_events_means_nothing_is_sent(async_client, db_session, ntfy_sends):
    calls, _ = ntfy_sends
    provider = await ntfy(db_session)
    await queue(db_session, provider, "Ancient", age=timedelta(hours=module.DIGEST_RETRY_HOURS + 1))

    await NotificationService().send_digest(provider.id)

    assert calls == []
    assert await queued(db_session, provider) == []


async def test_an_event_from_an_earlier_day_carries_its_date(async_client, db_session, ntfy_sends):
    calls, _ = ntfy_sends
    provider = await ntfy(db_session)
    await queue(db_session, provider, "Yesterday", age=timedelta(hours=30))

    await NotificationService().send_digest(provider.id)

    earlier = datetime.utcnow() - timedelta(hours=30)
    assert earlier.strftime("%d.%m") in calls[-1]
