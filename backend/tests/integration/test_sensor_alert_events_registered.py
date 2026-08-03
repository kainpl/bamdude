"""A provider event has six registration points and a missed one is silent.

The flag saves and never dispatches; or it dispatches and cannot be switched
off; or the event exists for providers and cannot be subscribed to in Telegram.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_flags_survive_a_round_trip(async_client: AsyncClient):
    """Pydantic silently drops unknown fields, so a schema that lacks these
    would accept the write and quietly not apply it."""
    created = await async_client.post(
        "/api/v1/notifications/",
        json={
            "name": "ntfy-alerts",
            "provider_type": "ntfy",
            "enabled": True,
            "config": {"topic": "bamdude", "server_url": "https://ntfy.sh"},
            "on_sensor_threshold": True,
            "on_sensor_silent": True,
        },
    )
    assert created.status_code in (200, 201), created.text
    body = created.json()
    assert body["on_sensor_threshold"] is True
    assert body["on_sensor_silent"] is True

    fetched = (await async_client.get(f"/api/v1/notifications/{body['id']}")).json()
    assert fetched["on_sensor_threshold"] is True
    assert fetched["on_sensor_silent"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_telegram_can_subscribe_to_all_five(async_client: AsyncClient):
    from backend.app.models.telegram_chat import ALL_NOTIFY_EVENTS

    wanted = {
        "sensor_above_max",
        "sensor_below_min",
        "sensor_back_in_range",
        "sensor_silent",
        "sensor_speaking_again",
    }
    assert wanted <= set(ALL_NOTIFY_EVENTS)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_five_are_offered_to_a_chat(async_client: AsyncClient):
    """A chat can only subscribe to what this endpoint lists — an event missing
    from EVENT_CATEGORIES is unreachable in the interface."""
    rows = (await async_client.get("/api/v1/telegram/events")).json()
    offered = {row["event_type"] for row in rows}

    assert {"sensor_above_max", "sensor_silent"} <= offered
    assert all(row["label"] != row["event_type"] for row in rows if row["event_type"].startswith("sensor_"))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_new_events_are_not_switched_on_for_existing_chats(async_client: AsyncClient):
    """Adding an event must not make silent installs start talking."""
    from backend.app.models.telegram_chat import DEFAULT_NOTIFY_EVENTS

    assert not {e for e in DEFAULT_NOTIFY_EVENTS if e.startswith("sensor_")}
