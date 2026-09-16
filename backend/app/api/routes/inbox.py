"""The user's own in-app inbox: list, read, clear, subscriptions.

Spec: vault 60-specs/notification-center-spec §6. Every query is scoped to the
caller's ``user_id``; there is no administrative view of somebody else's inbox.
Static paths are declared before ``/{item_id}`` ones on purpose.
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.schemas.inbox import (
    BulkDeleteResult,
    BulkReadResult,
    InboxItem,
    InboxListResponse,
    InboxSubscriptionEvent,
    InboxSubscriptions,
    InboxSubscriptionsUpdate,
    UnreadCountResponse,
)
from backend.app.services import notification_inbox
from backend.app.services.notification_events import EVENT_CATALOG, catalog_rows, default_inbox_events

router = APIRouter(prefix="/inbox", tags=["inbox"])

Severity = Literal["info", "warning", "error"]


class InboxFilters:
    """The list, read-all and clear routes share one filter vocabulary (spec §6)."""

    def __init__(
        self,
        unread_only: bool = False,
        severity: Severity | None = None,
        printer_id: int | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ):
        self.unread_only = unread_only
        self.severity = severity
        self.printer_id = printer_id
        self.event_type = event_type
        self.since = since

    def apply(self, stmt, user_id: int):
        stmt = stmt.where(UserNotification.user_id == user_id)
        if self.unread_only:
            stmt = stmt.where(UserNotification.read_at.is_(None))
        if self.severity:
            stmt = stmt.where(UserNotification.severity == self.severity)
        if self.printer_id is not None:
            stmt = stmt.where(UserNotification.printer_id == self.printer_id)
        if self.event_type:
            stmt = stmt.where(UserNotification.event_type == self.event_type)
        if self.since is not None:
            # ⚠️ An AWARE value is CONVERTED to UTC, never stripped: the column is
            # UTC-naive (the whole DB is), so dropping "+03:00" off noon would ask
            # for noon UTC — three hours off. This filter is shared by list,
            # read-all AND clear, so the same skew would decide which rows are
            # marked read and which are DELETED. A naive value is left alone;
            # calling ``astimezone`` on it would assume the server's local zone
            # and introduce the mirror-image bug.
            value = self.since
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc)
            stmt = stmt.where(UserNotification.created_at >= value.replace(tzinfo=None))
        return stmt


def _require_user(current_user: User | None) -> User:
    if current_user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Authentication must be enabled to use the inbox")
    return current_user


async def _own_row(db: AsyncSession, user_id: int, item_id: int) -> UserNotification:
    row = (
        await db.execute(
            select(UserNotification).where(UserNotification.id == item_id, UserNotification.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Inbox item not found")
    return row


async def _subscriptions(db: AsyncSession, user_id: int) -> InboxSubscriptions:
    stored = (await db.execute(select(User.inbox_events).where(User.id == user_id))).scalar_one()
    active = set(stored if stored is not None else default_inbox_events())
    return InboxSubscriptions(
        is_default=stored is None,
        events=[
            InboxSubscriptionEvent(event_type=key, severity=meta.severity, group=meta.group, subscribed=key in active)
            for key, meta in catalog_rows()
        ],
    )


@router.get("/", response_model=InboxListResponse)
async def list_inbox(
    filters: InboxFilters = Depends(),
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    stmt = filters.apply(select(UserNotification), user.id)
    if before_id is not None:
        stmt = stmt.where(UserNotification.id < before_id)
    rows = (await db.execute(stmt.order_by(UserNotification.id.desc()).limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return InboxListResponse(
        items=[InboxItem.from_row(r) for r in rows],
        unread_count=await notification_inbox.unread_count(db, user.id),
        next_before_id=rows[-1].id if has_more and rows else None,
    )


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    return UnreadCountResponse(unread_count=await notification_inbox.unread_count(db, user.id))


@router.post("/read-all", response_model=BulkReadResult)
async def mark_all_read(
    filters: InboxFilters = Depends(),
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    stmt = filters.apply(update(UserNotification), user.id).where(UserNotification.read_at.is_(None))
    result = await db.execute(stmt.values(read_at=datetime.utcnow()))
    await db.commit()
    return BulkReadResult(updated=int(result.rowcount or 0))


@router.get("/subscriptions", response_model=InboxSubscriptions)
async def get_subscriptions(
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    return await _subscriptions(db, user.id)


@router.put("/subscriptions", response_model=InboxSubscriptions)
async def update_subscriptions(
    data: InboxSubscriptionsUpdate,
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    events: list[str] | None = None
    if data.events is not None:
        unknown = sorted(set(data.events) - set(EVENT_CATALOG))
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Unknown inbox event type: {', '.join(unknown)}"
            )
        events = sorted(set(data.events))
    await db.execute(update(User).where(User.id == user.id).values(inbox_events=events))
    await db.commit()
    return await _subscriptions(db, user.id)


@router.post("/{item_id}/read", response_model=InboxItem)
async def mark_read(
    item_id: int,
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    row = await _own_row(db, user.id, item_id)
    if row.read_at is None:
        row.read_at = datetime.utcnow()
        await db.commit()
        await db.refresh(row)
    return InboxItem.from_row(row)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: int,
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    row = await _own_row(db, user.id, item_id)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/", response_model=BulkDeleteResult)
async def clear_inbox(
    filters: InboxFilters = Depends(),
    current_user: User | None = RequirePermission(Permission.NOTIFICATIONS_INBOX),
    db: AsyncSession = Depends(get_db),
):
    user = _require_user(current_user)
    result = await db.execute(filters.apply(delete(UserNotification), user.id))
    await db.commit()
    return BulkDeleteResult(deleted=int(result.rowcount or 0))
