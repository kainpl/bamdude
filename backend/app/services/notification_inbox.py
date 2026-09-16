"""The in-app inbox as a delivery channel — not a NotificationProvider row.

Spec: vault 60-specs/notification-center-spec §5. ``INBOX_CHANNEL`` rides the list
``NotificationService._get_providers_for_event`` returns, which keeps the 32
``if not providers: return`` guards in the service truthful without touching
them; ``_send_to_providers`` delivers it FIRST (before any network provider) and
skips the provider bookkeeping — no ``notification_logs`` row, no digest, no
quiet hours — because none of that describes an inbox.

``deliver`` commits the caller's session, exactly as ``_log_notification`` does;
opening a second session here would take a second SQLite connection against a
possibly open write transaction and earn a ``database is locked``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.websocket import ws_manager
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.schemas.inbox import InboxItem
from backend.app.services.notification_events import event_meta, wants_inbox_event

logger = logging.getLogger(__name__)


class InboxChannel:
    """Quacks like a provider just enough for the dispatcher's loop and the progress floor."""

    id = None
    name = "inbox"
    provider_type = "inbox"
    enabled = True
    printer_id = None
    daily_digest_enabled = False
    daily_digest_time = None
    progress_min_duration_minutes = 0

    def __repr__(self) -> str:
        return "<InboxChannel>"


INBOX_CHANNEL = InboxChannel()


async def recipients(db: AsyncSession, event_type: str) -> list[int]:
    """Active users whose subscription (NULL = defaults) contains ``event_type``; columns read by name."""
    rows = (await db.execute(select(User.id, User.is_active, User.inbox_events))).all()
    return [r.id for r in rows if r.is_active and wants_inbox_event(r.inbox_events, event_type)]


async def unread_count(db: AsyncSession, user_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(UserNotification)
        .where(UserNotification.user_id == user_id, UserNotification.read_at.is_(None))
    )
    return int((await db.execute(stmt)).scalar_one())


async def deliver(
    db: AsyncSession,
    *,
    event_type: str,
    title: str,
    message: str,
    printer_id: int | None = None,
    printer_name: str | None = None,
    extra_data: dict | None = None,
) -> list[int]:
    """Fan the rendered event out to every subscribed user; one commit; then one live update each.

    Returns the recipient ids. Payloads are built BEFORE the commit so nothing is
    read back from an expired instance afterwards.
    """
    meta = event_meta(event_type)
    user_ids = await recipients(db, event_type)
    if not user_ids:
        return []

    now = datetime.utcnow()
    rows = [
        UserNotification(
            user_id=uid,
            event_type=event_type,
            severity=meta.severity,
            title=title[:255],
            message=message,
            printer_id=printer_id,
            printer_name=printer_name,
            extra_data=extra_data,
            created_at=now,
        )
        for uid in user_ids
    ]
    db.add_all(rows)
    await db.flush()
    payloads = [(row.user_id, InboxItem.from_row(row).model_dump(mode="json")) for row in rows]
    await db.commit()

    for uid, item in payloads:
        try:
            count = await unread_count(db, uid)
            await ws_manager.broadcast_to_user(
                uid, {"type": "inbox_item", "data": {"item": item, "unread_count": count}}
            )
        except Exception:
            logger.exception("Inbox: live update for user %s failed", uid)
    return user_ids


async def prune_older_than(db: AsyncSession, days: int) -> int:
    """Retention sweep: rows older than ``days`` go, read or not (spec §4.4)."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days)))
    result = await db.execute(delete(UserNotification).where(UserNotification.created_at < cutoff))
    await db.commit()
    return int(result.rowcount or 0)
