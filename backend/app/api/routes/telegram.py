"""Telegram chat management API routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.telegram_chat import (
    ALL_NOTIFY_EVENTS,
    DEFAULT_NOTIFY_EVENTS,
    TelegramChat,
)
from backend.app.models.user import User
from backend.app.schemas.telegram import (
    NotifyEventInfo,
    TelegramChatCreate,
    TelegramChatResponse,
    TelegramChatUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _to_response(chat: TelegramChat) -> TelegramChatResponse:
    """Convert model to response schema."""
    return TelegramChatResponse(
        id=chat.id,
        chat_id=chat.chat_id,
        label=chat.label,
        group_id=chat.group_id,
        group_name=chat.group.name if chat.group else None,
        user_id=chat.user_id,
        username=chat.user.username if chat.user else None,
        is_active=chat.is_active,
        notify_events=chat.notify_events,
        daily_digest=chat.daily_digest,
        quiet_hours_enabled=chat.quiet_hours_enabled,
        quiet_hours_start=chat.quiet_hours_start,
        quiet_hours_end=chat.quiet_hours_end,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


# Event type metadata for the frontend
EVENT_CATEGORIES = {
    "print_lifecycle": {
        "label": "Print Lifecycle",
        "events": [
            "print_start",
            "print_complete",
            "print_failed",
            "print_stopped",
            "print_progress",
            "print_missing_spool_assignment",
        ],
    },
    "printer_status": {
        "label": "Printer Status",
        "events": [
            "printer_offline",
            "printer_error",
            "filament_low",
            "maintenance_due",
        ],
    },
    "ams": {
        "label": "AMS Environmental",
        "events": [
            "ams_humidity_high",
            "ams_temperature_high",
            "ams_ht_humidity_high",
            "ams_ht_temperature_high",
        ],
    },
    "print_events": {
        "label": "Print Events",
        "events": [
            "plate_not_empty",
            "bed_cooled",
            "first_layer_complete",
        ],
    },
    "queue": {
        "label": "Queue",
        "events": [
            "queue_job_added",
            "queue_job_assigned",
            "queue_job_started",
            "queue_job_waiting",
            "queue_job_skipped",
            "queue_job_failed",
            "queue_completed",
        ],
    },
}

EVENT_LABELS = {
    "print_start": "Print started",
    "print_complete": "Print completed",
    "print_failed": "Print failed",
    "print_stopped": "Print stopped",
    "print_progress": "Print progress (25/50/75%)",
    "print_missing_spool_assignment": "Missing spool assignment",
    "printer_offline": "Printer offline",
    "printer_error": "Printer error",
    "filament_low": "Filament low",
    "maintenance_due": "Maintenance due",
    "ams_humidity_high": "AMS humidity high",
    "ams_temperature_high": "AMS temperature high",
    "ams_ht_humidity_high": "AMS-HT humidity high",
    "ams_ht_temperature_high": "AMS-HT temperature high",
    "plate_not_empty": "Plate not empty",
    "bed_cooled": "Bed cooled",
    "first_layer_complete": "First layer complete",
    "queue_job_added": "Queue job added",
    "queue_job_assigned": "Queue job assigned",
    "queue_job_started": "Queue job started",
    "queue_job_waiting": "Queue job waiting",
    "queue_job_skipped": "Queue job skipped",
    "queue_job_failed": "Queue job failed",
    "queue_completed": "Queue completed",
}


@router.get("/chats", response_model=list[TelegramChatResponse])
async def list_chats(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """List all registered Telegram chats."""
    result = await db.execute(select(TelegramChat).order_by(TelegramChat.created_at))
    chats = list(result.scalars().all())
    return [_to_response(c) for c in chats]


@router.post("/chats", response_model=TelegramChatResponse, status_code=201)
async def create_chat(
    data: TelegramChatCreate,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Register a new Telegram chat."""
    # Check duplicate
    existing = await db.execute(
        select(TelegramChat).where(TelegramChat.chat_id == data.chat_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Chat ID already registered")

    # Validate group
    if data.group_id:
        group = await db.get(Group, data.group_id)
        if not group:
            raise HTTPException(400, "Group not found")

    # Validate user
    if data.user_id:
        user = await db.get(User, data.user_id)
        if not user:
            raise HTTPException(400, "User not found")

    # Validate notify_events
    if data.notify_events is not None:
        invalid = set(data.notify_events) - set(ALL_NOTIFY_EVENTS)
        if invalid:
            raise HTTPException(400, f"Invalid event types: {', '.join(invalid)}")

    chat = TelegramChat(
        chat_id=data.chat_id,
        label=data.label,
        group_id=data.group_id,
        user_id=data.user_id,
        is_active=data.is_active,
        notify_events=data.notify_events,
    )
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    return _to_response(chat)


@router.put("/chats/{chat_db_id}", response_model=TelegramChatResponse)
async def update_chat(
    chat_db_id: int,
    data: TelegramChatUpdate,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Update a Telegram chat."""
    chat = await db.get(TelegramChat, chat_db_id)
    if not chat:
        raise HTTPException(404, "Chat not found")

    update_data = data.model_dump(exclude_unset=True)

    if "group_id" in update_data and update_data["group_id"] is not None:
        group = await db.get(Group, update_data["group_id"])
        if not group:
            raise HTTPException(400, "Group not found")

    if "user_id" in update_data and update_data["user_id"] is not None:
        user = await db.get(User, update_data["user_id"])
        if not user:
            raise HTTPException(400, "User not found")

    if "notify_events" in update_data and update_data["notify_events"] is not None:
        invalid = set(update_data["notify_events"]) - set(ALL_NOTIFY_EVENTS)
        if invalid:
            raise HTTPException(400, f"Invalid event types: {', '.join(invalid)}")

    for key, value in update_data.items():
        setattr(chat, key, value)

    await db.commit()
    await db.refresh(chat)
    return _to_response(chat)


@router.delete("/chats/{chat_db_id}", status_code=204)
async def delete_chat(
    chat_db_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Remove a Telegram chat."""
    chat = await db.get(TelegramChat, chat_db_id)
    if not chat:
        raise HTTPException(404, "Chat not found")
    await db.delete(chat)
    await db.commit()


@router.post("/chats/{chat_db_id}/test")
async def test_chat(
    chat_db_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Send a test message to a Telegram chat."""
    chat = await db.get(TelegramChat, chat_db_id)
    if not chat:
        raise HTTPException(404, "Chat not found")

    from backend.app.i18n import escape_md
    from backend.app.services.telegram_bot import send_message

    text = escape_md("Test message from Bambuddy HE. If you see this, the chat is connected!")
    ok = await send_message(chat.chat_id, f"\u2705 {text}")
    if not ok:
        raise HTTPException(500, "Failed to send message. Is the bot running?")

    return {"status": "sent"}


@router.get("/events", response_model=list[NotifyEventInfo])
async def list_events(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """List all available notification event types with labels and categories."""
    result = []
    for category_key, category_data in EVENT_CATEGORIES.items():
        for event_type in category_data["events"]:
            result.append(NotifyEventInfo(
                event_type=event_type,
                category=category_data["label"],
                label=EVENT_LABELS.get(event_type, event_type),
                default=event_type in DEFAULT_NOTIFY_EVENTS,
            ))
    return result
