"""Settings, permissions and status routes for OctoEverywhere detection.

Covers what upstream Bambuddy's ``test_octoeverywhere_api`` covers, on BamDude's settings
credentials (a write-only key with presence) and permission gate.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.octoeverywhere_detection import octoeverywhere_detection_service as service

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def detection_reads_the_test_database(test_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(
        "backend.app.services.octoeverywhere_detection.async_session",
        async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
    )


@pytest.fixture(autouse=True)
def clear_detection_state():
    def clear():
        service._states.clear()
        service._history.clear()
        service._last_error = None
        service._last_error_code = None
        service._blocked_api_key = None
        service._blocked_error_code = None

    clear()
    # A save refreshes the running service; no loop runs in these tests.
    with patch.object(service, "refresh_settings", AsyncMock()):
        yield
    clear()


async def test_settings_round_trip_and_provider_switch(async_client, printer_factory):
    first = await printer_factory()
    second = await printer_factory()
    settings = (await async_client.get("/api/v1/settings/")).json()
    assert settings["octoeverywhere_enabled"] is False
    assert "octoeverywhere_api_key" not in settings
    assert settings["octoeverywhere_api_key_configured"] is False
    assert settings["octoeverywhere_confidence"] == "medium"
    assert settings["octoeverywhere_poll_interval"] == 20
    assert settings["octoeverywhere_action"] == "notify"

    await async_client.put("/api/v1/settings/", json={"obico_enabled": True})
    response = await async_client.put(
        "/api/v1/settings/",
        json={
            "octoeverywhere_enabled": True,
            "octoeverywhere_api_key": "  test-key  ",
            "octoeverywhere_confidence": "high",
            "octoeverywhere_action": "pause",
            "octoeverywhere_enabled_printers": json.dumps([second.id, first.id]),
            "octoeverywhere_poll_interval": 15,
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert "octoeverywhere_api_key" not in saved
    assert saved["octoeverywhere_api_key_configured"] is True
    assert saved["obico_enabled"] is False  # enabling one provider switches the other off

    status = (await async_client.get("/api/v1/octoeverywhere/status")).json()
    assert status["enabled"] is True
    assert status["api_key_configured"] is True
    assert (status["confidence"], status["action"], status["poll_interval"]) == ("high", "pause", 15)
    loaded = await service._load_settings()
    assert loaded["api_key"] == "test-key"
    assert loaded["enabled_printers"] == {first.id, second.id}

    back = (await async_client.put("/api/v1/settings/", json={"obico_enabled": True})).json()
    assert back["obico_enabled"] is True and back["octoeverywhere_enabled"] is False


async def test_two_enabled_providers_are_refused(async_client):
    response = await async_client.put("/api/v1/settings/", json={"obico_enabled": True, "octoeverywhere_enabled": True})
    assert response.status_code == 400


async def test_the_key_is_kept_replaced_and_cleared(async_client):
    await async_client.put("/api/v1/settings/", json={"octoeverywhere_api_key": "first-key"})
    await async_client.put("/api/v1/settings/", json={"octoeverywhere_confidence": "low"})
    assert (await service._load_settings())["api_key"] == "first-key"
    await async_client.put("/api/v1/settings/", json={"octoeverywhere_api_key": "second-key"})
    assert (await service._load_settings())["api_key"] == "second-key"
    cleared = await async_client.put("/api/v1/settings/", json={"secret_actions": {"octoeverywhere_api_key": "clear"}})
    assert cleared.json()["octoeverywhere_api_key_configured"] is False
    assert (await service._load_settings())["api_key"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"octoeverywhere_confidence": "extreme"},
        {"octoeverywhere_action": "explode"},
        {"octoeverywhere_poll_interval": 4},
        {"octoeverywhere_poll_interval": 31},
        {"octoeverywhere_enabled_printers": "[true]"},
        {"octoeverywhere_api_key": "with space"},
    ],
)
async def test_invalid_settings_rejected(async_client, payload):
    assert (await async_client.put("/api/v1/settings/", json=payload)).status_code == 422


async def test_a_save_refreshes_the_running_service(async_client):
    await async_client.put("/api/v1/settings/", json={"octoeverywhere_confidence": "high"})
    service.refresh_settings.assert_awaited()


async def test_connection_uses_saved_or_draft_key(async_client):
    await async_client.put(
        "/api/v1/settings/", json={"octoeverywhere_api_key": "saved-key", "octoeverywhere_confidence": "low"}
    )
    with patch.object(service, "test_connection", AsyncMock(return_value={"ok": True})) as tested:
        assert (await async_client.post("/api/v1/octoeverywhere/test-connection", json={})).json() == {"ok": True}
        tested.assert_awaited_with("saved-key", "low")
        await async_client.post(
            "/api/v1/octoeverywhere/test-connection",
            json={
                "api_key": "draft-key",
                "confidence": "high",
                "secret_actions": {"octoeverywhere_api_key": "replace"},
            },
        )
        tested.assert_awaited_with("draft-key", "high")
    # The draft is not saved by a test.
    assert (await service._load_settings())["api_key"] == "saved-key"


async def test_connection_refuses_a_value_with_keep(async_client):
    response = await async_client.post(
        "/api/v1/octoeverywhere/test-connection",
        json={"api_key": "x", "secret_actions": {"octoeverywhere_api_key": "keep"}},
    )
    assert response.status_code == 422


async def test_usage_limit_code_reaches_status_and_printer_status(async_client):
    service._block_account("key", "OE_FREE_USAGE_LIMIT_REACHED")
    status = (await async_client.get("/api/v1/octoeverywhere/status")).json()
    assert status["last_error_code"] == "OE_FREE_USAGE_LIMIT_REACHED"
    cards = (await async_client.get("/api/v1/octoeverywhere/printer-status")).json()
    assert cards["last_error_code"] == "OE_FREE_USAGE_LIMIT_REACHED"


@pytest.mark.parametrize("can_see_error", [True, False])
async def test_printer_status_hides_the_reason_without_settings_read(can_see_error):
    from backend.app.api.routes.octoeverywhere import get_printer_status
    from backend.app.services.octoeverywhere_detection import _PrintState

    service._states[7] = _PrintState(verdict="warning", error="Could not reach OctoEverywhere.")
    user = SimpleNamespace(has_permission=lambda permission: can_see_error)
    result = await get_printer_status(user=user, request=None)
    entry = result["per_printer"][7]
    assert entry["class"] == "error"  # the class stays: the print is not being watched
    assert (entry["error"] is not None) is can_see_error
    assert (result["last_error"] is not None) is can_see_error or service._last_error is None
