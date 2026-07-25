"""Tests for Pushover emergency-priority (2) retry/expire handling (#2586).

Pushover rejects a priority-2 (Emergency) message unless it also carries
``retry`` and ``expire``. These tests pin that we send those params at
priority 2 (clamped to Pushover's legal 30..10800 range) and omit them at
every other priority.

The string-value test is not academic: the provider config round-trips as an
untyped JSON blob (``models/notification.py``) and the frontend modal writes
every field as a string even for ``type="number"``, so ``priority`` really does
arrive as ``"2"`` in production.
"""

import httpx
import pytest

from backend.app.services.notification_service import NotificationService


class _CaptureClient:
    """Minimal stand-in for httpx.AsyncClient that records the posted data."""

    def __init__(self):
        self.is_closed = False
        self.last_data: dict | None = None

    async def post(self, url, data=None, files=None):
        self.last_data = data
        return httpx.Response(200, json={"status": 1})


@pytest.fixture
def svc():
    service = NotificationService()
    client = _CaptureClient()
    service._http_client = client
    return service, client


def _cfg(**extra):
    return {"user_key": "u" * 30, "app_token": "a" * 30, **extra}


@pytest.mark.asyncio
async def test_emergency_priority_sends_retry_and_expire(svc):
    service, client = svc
    ok, _ = await service._send_pushover(_cfg(priority=2), "t", "m")
    assert ok
    assert client.last_data["priority"] == 2
    assert client.last_data["retry"] == 60
    assert client.last_data["expire"] == 3600


@pytest.mark.asyncio
async def test_emergency_priority_arrives_as_a_string(svc):
    """The config is an untyped JSON blob and the modal writes strings."""
    service, client = svc
    ok, _ = await service._send_pushover(_cfg(priority="2"), "t", "m")
    assert ok
    assert client.last_data["priority"] == 2
    assert client.last_data["retry"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", [-2, -1, 0, 1, "1"])
async def test_other_priorities_omit_retry_and_expire(svc, priority):
    service, client = svc
    await service._send_pushover(_cfg(priority=priority), "t", "m")
    assert "retry" not in client.last_data
    assert "expire" not in client.last_data


@pytest.mark.asyncio
async def test_user_values_are_clamped_to_pushovers_window(svc):
    service, client = svc
    await service._send_pushover(_cfg(priority=2, retry=5, expire=999999), "t", "m")
    assert client.last_data["retry"] == 30  # floor
    assert client.last_data["expire"] == 10800  # ceiling


@pytest.mark.asyncio
async def test_non_numeric_values_fall_back_to_defaults(svc):
    service, client = svc
    await service._send_pushover(_cfg(priority=2, retry="soon", expire=None), "t", "m")
    assert client.last_data["retry"] == 60
    assert client.last_data["expire"] == 3600


@pytest.mark.asyncio
async def test_unparseable_priority_degrades_to_normal(svc):
    """A pasted "2.0" or junk value must not crash the send — it drops to 0,
    which also means no retry/expire is sent."""
    service, client = svc
    await service._send_pushover(_cfg(priority="2.0"), "t", "m")
    assert client.last_data["priority"] == 0
    assert "retry" not in client.last_data
