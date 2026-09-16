"""Wire shapes of the in-app inbox (spec: vault 60-specs/notification-center-spec §6, §7)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.app.services.notification_events import event_meta


class InboxItem(BaseModel):
    """One inbox row as the page and the WebSocket both see it — ONE shape, built by ``from_row``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    severity: str
    group: str
    title: str
    message: str
    printer_id: int | None = None
    printer_name: str | None = None
    extra_data: dict | None = None
    created_at: datetime
    read_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "InboxItem":
        return cls(
            id=row.id,
            event_type=row.event_type,
            severity=row.severity,
            group=event_meta(row.event_type).group,
            title=row.title,
            message=row.message,
            printer_id=row.printer_id,
            printer_name=row.printer_name,
            extra_data=row.extra_data,
            created_at=row.created_at,
            read_at=row.read_at,
        )


class InboxListResponse(BaseModel):
    items: list[InboxItem]
    unread_count: int
    next_before_id: int | None = None


class UnreadCountResponse(BaseModel):
    unread_count: int


class BulkReadResult(BaseModel):
    updated: int


class BulkDeleteResult(BaseModel):
    deleted: int


class InboxSubscriptionEvent(BaseModel):
    event_type: str
    severity: str
    group: str
    subscribed: bool


class InboxSubscriptions(BaseModel):
    is_default: bool
    events: list[InboxSubscriptionEvent]


class InboxSubscriptionsUpdate(BaseModel):
    events: list[str] | None
