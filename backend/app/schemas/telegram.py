"""Pydantic schemas for Telegram chat management."""

from datetime import datetime

from pydantic import BaseModel, Field


class TelegramChatCreate(BaseModel):
    """Create a new Telegram chat."""

    chat_id: int = Field(description="Telegram chat ID")
    label: str | None = Field(default=None, description="Display name")
    group_id: int | None = Field(default=None, description="Group (role) ID")
    user_id: int | None = Field(default=None, description="Linked system user ID")
    is_active: bool = Field(default=False, description="Whether the chat is active")
    notify_events: list[str] | None = Field(default=None, description="Notification event types (null = defaults)")
    daily_digest: bool = Field(default=False, description="Receive daily digest")
    quiet_hours_enabled: bool = Field(default=False, description="Enable quiet hours")
    quiet_hours_start: str | None = Field(default=None, description="Quiet hours start (HH:MM)")
    quiet_hours_end: str | None = Field(default=None, description="Quiet hours end (HH:MM)")


class TelegramChatUpdate(BaseModel):
    """Update a Telegram chat."""

    label: str | None = None
    group_id: int | None = None
    user_id: int | None = None
    is_active: bool | None = None
    notify_events: list[str] | None = None
    daily_digest: bool | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class TelegramChatResponse(BaseModel):
    """Response schema for a Telegram chat."""

    id: int
    chat_id: int
    label: str | None
    group_id: int | None
    group_name: str | None = None
    user_id: int | None
    username: str | None = None
    is_active: bool
    notify_events: list[str] | None
    daily_digest: bool
    quiet_hours_enabled: bool
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotifyEventInfo(BaseModel):
    """Info about a notification event type."""

    event_type: str
    category: str
    label: str
    default: bool = False
