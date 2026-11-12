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

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.websocket import ws_manager
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.schemas.inbox import InboxItem
from backend.app.services.notification_events import IN_APP_ONLY_EVENTS, event_meta, wants_inbox_event

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
    """Active users whose subscription (NULL = defaults) contains ``event_type``; columns read by name.

    ⚠️ Under ``no_autoflush``, for the reason ``_get_providers_for_event`` and
    ``_get_template`` give: a caller routinely holds pending writes when it
    raises an event, and autoflush would satisfy this SELECT by writing them,
    opening a write transaction on SQLite. It bites hardest when nobody is
    subscribed — ``deliver`` then returns WITHOUT committing, so that open
    transaction is handed straight to the SMTP/ntfy timeout and unrelated
    background tasks die with "database is locked". Users are committed rows,
    so a pending change in the caller's session is never one this query wants.
    """
    with db.no_autoflush:
        rows = (await db.execute(select(User.id, User.is_active, User.inbox_events))).all()
    return [r.id for r in rows if r.is_active and wants_inbox_event(r.inbox_events, event_type)]


async def has_subscriber(db: AsyncSession, event_type: str) -> bool:
    """Is anybody at all subscribed to ``event_type``? — the cheap form of ``recipients``.

    For a caller deciding whether the WORK behind an event is worth starting:
    ``main.py``'s bed-cooldown monitor polls a printer for up to 30 minutes to
    produce one ``bed_cooled`` row, and must not run that loop for an empty
    inbox. The NULL-means-defaults rule lives in Python (``wants_inbox_event``),
    so only the ``is_active`` half can be asked of SQL. ``no_autoflush`` for the
    same reason as ``recipients``.
    """
    with db.no_autoflush:
        rows = (await db.execute(select(User.inbox_events).where(User.is_active.is_(True)))).scalars().all()
    return any(wants_inbox_event(subscription, event_type) for subscription in rows)


async def unread_count(db: AsyncSession, user_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(UserNotification)
        .where(UserNotification.user_id == user_id, UserNotification.read_at.is_(None))
    )
    return int((await db.execute(stmt)).scalar_one())


async def _unread_counts(db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    """Unread totals for several users in ONE round trip — ``deliver``'s fan-out.

    A ``SELECT count(*)`` per recipient is the same numbers at N times the cost.
    """
    stmt = (
        select(UserNotification.user_id, func.count())
        .where(UserNotification.user_id.in_(user_ids), UserNotification.read_at.is_(None))
        .group_by(UserNotification.user_id)
    )
    return {int(uid): int(total) for uid, total in (await db.execute(stmt)).all()}


@dataclass(frozen=True)
class StagedInboxItem:
    user_id: int
    account_identity: str
    item: dict
    unread_count: int


@dataclass(frozen=True)
class StagedInboxBatch:
    items: tuple[StagedInboxItem, ...] = ()


async def stage_admin_event(
    db: AsyncSession, *, event_type: str, episode_id: str, title: str, message: str, extra_data: dict | None = None
) -> StagedInboxBatch:
    """Stage an internal admin notice in the caller's episode transaction.

    Caller locks its durable episode FIRST (SQLite writer / PG FOR UPDATE),
    then this operation locks accounts in the shared lifetime order. No commit,
    nested session, provider, or live send. E can reuse this with its own episode.
    """
    from backend.app.core.auth_lifetime import AccountCapture, guard_accounts

    if event_type not in IN_APP_ONLY_EVENTS:
        raise ValueError("Only internal events can be staged")
    with db.no_autoflush:
        candidates = (
            (await db.execute(select(User).where(User.is_active.is_(True)).execution_options(populate_existing=True)))
            .scalars()
            .all()
        )
    # Lock even currently ineligible accounts: group/subscription mutation must
    # not race the authoritative eligibility read below.
    captures = [AccountCapture(u.account_identity, None, u.id) for u in candidates]
    if captures:
        with db.no_autoflush:
            await guard_accounts(db, captures)
    with db.no_autoflush:
        users = (
            (
                await db.execute(
                    select(User)
                    .where(User.id.in_([c.user_id for c in captures]))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    identities = {c.user_id: c.account_identity for c in captures}
    eligible = [
        u
        for u in users
        if u.account_identity == identities[u.id]
        and u.is_active
        and u.is_admin
        and u.has_permission("notifications:inbox")
        and wants_inbox_event(u.inbox_events, event_type)
    ]
    keys = {u.id: f"{event_type}:{episode_id}:{u.account_identity}" for u in eligible}
    with db.no_autoflush:
        existing = (
            set(
                (
                    await db.execute(
                        select(UserNotification.dedupe_key).where(UserNotification.dedupe_key.in_(list(keys.values())))
                    )
                ).scalars()
            )
            if keys
            else set()
        )
    now = datetime.utcnow()
    meta = event_meta(event_type)
    rows = [
        UserNotification(
            user_id=u.id,
            event_type=event_type,
            severity=meta.severity,
            title=title[:255],
            message=message,
            extra_data=extra_data,
            created_at=now,
            dedupe_key=keys[u.id],
        )
        for u in eligible
        if keys[u.id] not in existing
    ]
    db.add_all(rows)
    await db.flush()
    counts = await _unread_counts(db, [r.user_id for r in rows]) if rows else {}
    return StagedInboxBatch(
        tuple(
            StagedInboxItem(
                r.user_id,
                identities[r.user_id],
                InboxItem.from_row(r).model_dump(mode="json"),
                counts.get(r.user_id, 0),
            )
            for r in rows
        )
    )


async def dispatch_staged(batch: StagedInboxBatch, *, seconds: float = 1.0) -> None:
    """Best-effort postcommit live hints. Scalars only; never opens SQL."""
    try:
        async with asyncio.timeout(seconds):
            for item in batch.items:
                await ws_manager.broadcast_to_user(
                    item.user_id,
                    {"type": "inbox_item", "data": {"item": item.item, "unread_count": item.unread_count}},
                    expected_account_identity=item.account_identity,
                )
    except Exception:
        logger.warning("Inbox: staged live update failed")


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
    read back from an expired instance afterwards. ``title`` is truncated to the
    column's 255 characters — an inbox headline is a headline, and a refusal
    here would lose a notification over a long filename.
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

    counts = await _unread_counts(db, user_ids)
    for uid, item in payloads:
        try:
            await ws_manager.broadcast_to_user(
                uid, {"type": "inbox_item", "data": {"item": item, "unread_count": counts.get(uid, 0)}}
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
