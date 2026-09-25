"""The three scheduled-drying events reach providers through the ordinary path (spec §Сповіщення)."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS
from backend.app.models.telegram_chat import ALL_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS
from backend.app.services.notification_events import EVENT_CATALOG
from backend.app.services.notification_service import NotificationService


def test_severity_and_defaults():
    assert EVENT_CATALOG["scheduled_drying_started"].severity == "info"
    assert EVENT_CATALOG["scheduled_drying_completed"].severity == "info"
    assert EVENT_CATALOG["scheduled_drying_failed"].severity == "warning"
    assert PROVIDER_EVENT_DEFAULTS["on_scheduled_drying_started"] is False
    assert PROVIDER_EVENT_DEFAULTS["on_scheduled_drying_completed"] is False
    assert PROVIDER_EVENT_DEFAULTS["on_scheduled_drying_failed"] is True


def test_telegram_chats_hear_the_failure_by_default():
    assert {"scheduled_drying_started", "scheduled_drying_completed", "scheduled_drying_failed"} <= set(
        ALL_NOTIFY_EVENTS
    )
    assert "scheduled_drying_failed" in DEFAULT_NOTIFY_EVENTS
    assert "scheduled_drying_started" not in DEFAULT_NOTIFY_EVENTS


@pytest.mark.asyncio
async def test_failed_carries_the_reason():
    service = NotificationService()
    with (
        patch.object(service, "_get_providers_for_event", AsyncMock(return_value=["p"])),
        patch.object(service, "_build_message_from_template", AsyncMock(return_value=("t", "m"))) as build,
        patch.object(service, "_send_to_providers", AsyncMock()) as send,
    ):
        await service.on_scheduled_drying_failed(1, "X2D", "AMS-A", "its start window passed", "↻ 01:00", db=None)
    variables = build.await_args.args[2]
    assert variables["reason"] == "its start window passed"
    assert send.await_args.args[4] == "scheduled_drying_failed"


@pytest.mark.asyncio
async def test_started_carries_what_is_drying():
    service = NotificationService()
    with (
        patch.object(service, "_get_providers_for_event", AsyncMock(return_value=["p"])),
        patch.object(service, "_build_message_from_template", AsyncMock(return_value=("t", "m"))) as build,
        patch.object(service, "_send_to_providers", AsyncMock()),
    ):
        await service.on_scheduled_drying_started(1, "X2D", "AMS-A", "PLA", 55, 8, "", db=None)
    assert build.await_args.args[2] == {
        "printer": "X2D",
        "ams_label": "AMS-A",
        "filament": "PLA",
        "temp": "55",
        "hours": "8",
        "schedule": "",
    }
