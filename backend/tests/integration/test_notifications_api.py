"""Integration tests for Notifications API endpoints.

Tests the full request/response cycle for /api/v1/notifications/ endpoints.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


class TestNotificationsAPI:
    """Integration tests for /api/v1/notifications/ endpoints."""

    # ========================================================================
    # List endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_notification_providers_empty(self, async_client: AsyncClient):
        """Verify empty list is returned when no providers exist."""
        response = await async_client.get("/api/v1/notifications/")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_notification_providers_with_data(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify list returns existing providers."""
        _provider = await notification_provider_factory(name="Test Provider")

        response = await async_client.get("/api/v1/notifications/")

        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(p["name"] == "Test Provider" for p in data)

    # ========================================================================
    # Create endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_callmebot_provider(self, async_client: AsyncClient):
        """Verify callmebot notification provider can be created."""
        data = {
            "name": "Test CallMeBot",
            "provider_type": "callmebot",
            "enabled": True,
            "config": {"phone_number": "+1234567890", "api_key": "test-api-key"},
            "on_print_start": True,
            "on_print_complete": True,
            "on_print_failed": True,
            "on_print_stopped": False,
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "Test CallMeBot"
        assert result["provider_type"] == "callmebot"
        assert result["on_print_start"] is True
        assert result["on_print_stopped"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_ntfy_provider(self, async_client: AsyncClient):
        """Verify ntfy notification provider can be created."""
        data = {
            "name": "Test Ntfy",
            "provider_type": "ntfy",
            "enabled": True,
            "config": {
                "server": "https://ntfy.sh",
                "topic": "test-topic",
            },
            "on_print_complete": True,
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["provider_type"] == "ntfy"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_provider_with_printer(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify provider can be linked to specific printer."""
        printer = await printer_factory(name="Test Printer")

        data = {
            "name": "Printer Ntfy",
            "provider_type": "ntfy",
            "config": {"server": "https://ntfy.sh", "topic": "test-topic"},
            "printer_ids": [printer.id],
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["printer_ids"] == [printer.id]

    # ========================================================================
    # Get single endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_notification_provider(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify single provider can be retrieved."""
        provider = await notification_provider_factory(name="Get Test Provider")

        response = await async_client.get(f"/api/v1/notifications/{provider.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["id"] == provider.id
        assert result["name"] == "Get Test Provider"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_provider_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent provider."""
        response = await async_client.get("/api/v1/notifications/9999")

        assert response.status_code == 404

    # ========================================================================
    # Update endpoints (CRITICAL - toggle persistence)
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_event_toggles(self, async_client: AsyncClient, notification_provider_factory, db_session):
        """CRITICAL: Verify notification event toggles persist correctly."""
        provider = await notification_provider_factory(
            on_print_start=True,
            on_print_complete=True,
            on_print_stopped=False,
        )

        # Toggle on_print_stopped to True
        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"on_print_stopped": True})

        assert response.status_code == 200
        assert response.json()["on_print_stopped"] is True

        # Verify change persisted
        response = await async_client.get(f"/api/v1/notifications/{provider.id}")
        assert response.json()["on_print_stopped"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_ams_alarm_toggles(self, async_client: AsyncClient, notification_provider_factory, db_session):
        """CRITICAL: Verify AMS alarm toggles persist correctly."""
        provider = await notification_provider_factory(
            on_ams_humidity_high=False,
            on_ams_temperature_high=False,
        )

        # Enable AMS alarms
        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={
                "on_ams_humidity_high": True,
                "on_ams_temperature_high": True,
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["on_ams_humidity_high"] is True
        assert result["on_ams_temperature_high"] is True

        # Verify persistence
        response = await async_client.get(f"/api/v1/notifications/{provider.id}")
        result = response.json()
        assert result["on_ams_humidity_high"] is True
        assert result["on_ams_temperature_high"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_enable_disable_provider(self, async_client: AsyncClient, notification_provider_factory, db_session):
        """Verify provider can be enabled/disabled."""
        provider = await notification_provider_factory(enabled=True)

        # Disable
        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"enabled": False})

        assert response.status_code == 200
        assert response.json()["enabled"] is False

        # Enable
        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"enabled": True})

        assert response.status_code == 200
        assert response.json()["enabled"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_quiet_hours(self, async_client: AsyncClient, notification_provider_factory, db_session):
        """Verify quiet hours can be configured."""
        provider = await notification_provider_factory(quiet_hours_enabled=False)

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={
                "quiet_hours_enabled": True,
                "quiet_hours_start": "22:00",
                "quiet_hours_end": "07:00",
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["quiet_hours_enabled"] is True
        assert result["quiet_hours_start"] == "22:00"
        assert result["quiet_hours_end"] == "07:00"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_daily_digest(self, async_client: AsyncClient, notification_provider_factory, db_session):
        """Verify daily digest can be configured."""
        provider = await notification_provider_factory(daily_digest_enabled=False)

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={
                "daily_digest_enabled": True,
                "daily_digest_time": "09:00",
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["daily_digest_enabled"] is True
        assert result["daily_digest_time"] == "09:00"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_multiple_event_toggles(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify multiple event toggles can be updated at once."""
        provider = await notification_provider_factory(
            on_print_start=True,
            on_print_complete=True,
            on_print_failed=True,
            on_print_stopped=False,
            on_printer_offline=False,
        )

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={
                "on_print_start": False,
                "on_print_stopped": True,
                "on_printer_offline": True,
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["on_print_start"] is False
        assert result["on_print_stopped"] is True
        assert result["on_printer_offline"] is True
        # Unchanged fields should remain
        assert result["on_print_complete"] is True
        assert result["on_print_failed"] is True

    # ========================================================================
    # Test notification endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_test_notification(
        self, async_client: AsyncClient, notification_provider_factory, mock_httpx_client, db_session
    ):
        """Verify test notification can be sent."""
        provider = await notification_provider_factory()

        response = await async_client.post(f"/api/v1/notifications/{provider.id}/test")

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_test_notification_disabled_provider(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify test notification works even for disabled provider."""
        provider = await notification_provider_factory(enabled=False)

        response = await async_client.post(f"/api/v1/notifications/{provider.id}/test")

        # Test should still work for disabled providers
        assert response.status_code == 200

    # ========================================================================
    # Delete endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_notification_provider(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify notification provider can be deleted."""
        provider = await notification_provider_factory()
        provider_id = provider.id

        response = await async_client.delete(f"/api/v1/notifications/{provider_id}")

        assert response.status_code == 200

        # Verify deleted
        response = await async_client.get(f"/api/v1/notifications/{provider_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_nonexistent_provider(self, async_client: AsyncClient):
        """Verify deleting non-existent provider returns 404."""
        response = await async_client.delete("/api/v1/notifications/9999")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_provider_with_first_layer_complete(self, async_client: AsyncClient):
        """Verify first layer complete toggle persists on create."""
        data = {
            "name": "First Layer Test",
            "provider_type": "ntfy",
            "config": {"server": "https://ntfy.sh", "topic": "test"},
            "on_first_layer_complete": True,
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["on_first_layer_complete"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_first_layer_complete_toggle(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """CRITICAL: Verify first layer complete toggle persists correctly."""
        provider = await notification_provider_factory(on_first_layer_complete=False)

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"on_first_layer_complete": True},
        )

        assert response.status_code == 200
        assert response.json()["on_first_layer_complete"] is True

        # Verify persistence
        response = await async_client.get(f"/api/v1/notifications/{provider.id}")
        assert response.json()["on_first_layer_complete"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_first_layer_complete_independent_from_other_toggles(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify first layer complete is independent from bed cooled and print complete."""
        provider = await notification_provider_factory(
            on_print_complete=True,
            on_bed_cooled=False,
            on_first_layer_complete=True,
        )

        response = await async_client.get(f"/api/v1/notifications/{provider.id}")
        result = response.json()
        assert result["on_print_complete"] is True
        assert result["on_bed_cooled"] is False
        assert result["on_first_layer_complete"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_provider_with_missing_spool_assignment_toggle(self, async_client: AsyncClient):
        """Verify missing spool assignment toggle persists on create."""
        data = {
            "name": "Missing Spool Assignment Test",
            "provider_type": "ntfy",
            "config": {"server": "https://ntfy.sh", "topic": "test"},
            "on_print_missing_spool_assignment": True,
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["on_print_missing_spool_assignment"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_missing_spool_assignment_toggle(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """CRITICAL: Verify missing spool assignment toggle persists correctly."""
        provider = await notification_provider_factory(on_print_missing_spool_assignment=False)

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"on_print_missing_spool_assignment": True},
        )

        assert response.status_code == 200
        assert response.json()["on_print_missing_spool_assignment"] is True

        response = await async_client.get(f"/api/v1/notifications/{provider.id}")
        assert response.json()["on_print_missing_spool_assignment"] is True


class TestNotificationTemplatesAPI:
    """Integration tests for /api/v1/notification-templates/ endpoints."""

    @pytest.fixture
    async def seeded_templates(self, db_session):
        """Seed notification templates for tests."""
        from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate

        templates = []
        for template_data in DEFAULT_TEMPLATES:
            template = NotificationTemplate(**template_data)
            db_session.add(template)
            templates.append(template)
        await db_session.commit()
        for template in templates:
            await db_session.refresh(template)
        return templates

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_templates(self, async_client: AsyncClient, seeded_templates):
        """Verify default templates are seeded and can be listed."""
        response = await async_client.get("/api/v1/notification-templates/")

        assert response.status_code == 200
        templates = response.json()
        # Should have default templates seeded
        assert len(templates) >= 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_template_by_id(self, async_client: AsyncClient, seeded_templates):
        """Verify template can be retrieved by ID."""
        # Get first template ID from seeded data
        template_id = seeded_templates[0].id

        response = await async_client.get(f"/api/v1/notification-templates/{template_id}")

        assert response.status_code == 200
        template = response.json()
        assert template["id"] == template_id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_template(self, async_client: AsyncClient, seeded_templates):
        """Verify template can be updated."""
        # Get first template
        template_id = seeded_templates[0].id

        # Update it (route uses PUT, not PATCH)
        response = await async_client.put(
            f"/api/v1/notification-templates/{template_id}",
            json={
                "title_template": "Custom Title: {printer}",
                "body_template": "Custom body for {filename}",
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["title_template"] == "Custom Title: {printer}"
        assert result["body_template"] == "Custom body for {filename}"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reset_template_to_default(self, async_client: AsyncClient, seeded_templates):
        """Verify template can be reset to default."""
        template_id = seeded_templates[0].id

        response = await async_client.post(f"/api/v1/notification-templates/{template_id}/reset")

        assert response.status_code == 200
        result = response.json()
        assert result["is_default"] is True


class TestHomeAssistantNotificationProvider:
    """Integration tests for Home Assistant notification provider."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_homeassistant_provider(self, async_client: AsyncClient):
        """Verify homeassistant notification provider can be created with empty config."""
        data = {
            "name": "HA Notifications",
            "provider_type": "homeassistant",
            "enabled": True,
            "config": {},
            "on_print_complete": True,
            "on_print_failed": True,
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "HA Notifications"
        assert result["provider_type"] == "homeassistant"
        assert result["on_print_complete"] is True
        assert result["on_print_failed"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_homeassistant_provider(
        self, async_client: AsyncClient, notification_provider_factory, db_session
    ):
        """Verify homeassistant provider can be updated."""
        provider = await notification_provider_factory(
            name="HA Test",
            provider_type="homeassistant",
            config="{}",
        )

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"on_print_start": True, "on_printer_offline": True},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["on_print_start"] is True
        assert result["on_printer_offline"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_test_homeassistant_config_without_ha_settings(self, async_client: AsyncClient):
        """Verify test-config returns error when HA is not configured."""
        response = await async_client.post(
            "/api/v1/notifications/test-config",
            json={"provider_type": "homeassistant", "config": {}},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is False
        assert "not configured" in result["message"].lower() or "Home Assistant" in result["message"]


class TestSignalNotificationProvider:
    """Integration tests for the Signal (signal-cli-rest-api) notification provider."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_signal_provider(self, async_client: AsyncClient):
        """Verify a signal notification provider can be created."""
        data = {
            "name": "Signal Notifications",
            "provider_type": "signal",
            "enabled": True,
            "config": {
                "server": "http://localhost:8080",
                "sender_number": "+15550000000",
                "recipient_type": "numbers",
                "numbers": "+15551111111",
            },
            "on_print_complete": True,
            "on_print_failed": True,
        }

        response = await async_client.post("/api/v1/notifications/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "Signal Notifications"
        assert result["provider_type"] == "signal"
        assert result["config"]["sender_number"] == "+15550000000"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_signal_provider(self, async_client: AsyncClient, notification_provider_factory, db_session):
        """Verify a signal provider's recipient config can be switched to a group."""
        provider = await notification_provider_factory(
            name="Signal Test",
            provider_type="signal",
            config='{"server": "http://localhost:8080", "sender_number": "+15550000000", "recipient_type": "numbers", "numbers": "+15551111111"}',
        )

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={
                "config": {
                    "server": "http://localhost:8080",
                    "sender_number": "+15550000000",
                    "recipient_type": "group",
                    "group_id": "group.abc123==",
                }
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["config"]["recipient_type"] == "group"
        assert result["config"]["group_id"] == "group.abc123=="

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_test_signal_config_without_server(self, async_client: AsyncClient):
        """Verify test-config returns a clear error when the Signal API URL is missing."""
        response = await async_client.post(
            "/api/v1/notifications/test-config",
            json={"provider_type": "signal", "config": {"sender_number": "+15550000000", "numbers": "+15551111111"}},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is False
        assert "Signal API URL" in result["message"]


class TestProviderEventsEndpoint:
    """``GET /notifications/events`` — the list the provider form renders from.

    It exists because three hand-kept copies on the frontend drifted: the
    dialog offered 18 of the 34 flags, and six of the sixteen it hid default to
    ON, so a new provider quietly sent events nobody had been shown. These
    tests pin the contract that makes a hand-kept copy unnecessary.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_every_provider_flag_is_listed_exactly_once(self, async_client: AsyncClient):
        from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS

        response = await async_client.get("/api/v1/notifications/events")

        assert response.status_code == 200
        rows = response.json()
        flags = [row["flag"] for row in rows]
        assert len(flags) == len(set(flags))
        assert set(flags) == set(PROVIDER_EVENT_DEFAULTS)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_defaults_are_the_registry_defaults(self, async_client: AsyncClient):
        """What the form pre-ticks has to be what the backend would store anyway."""
        from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS

        rows = (await async_client.get("/api/v1/notifications/events")).json()

        assert {row["flag"]: row["default"] for row in rows} == PROVIDER_EVENT_DEFAULTS

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_every_row_carries_catalogued_events_severity_and_group(self, async_client: AsyncClient):
        from backend.app.services.notification_events import EVENT_CATALOG, GROUPS, SEVERITIES

        rows = (await async_client.get("/api/v1/notifications/events")).json()

        for row in rows:
            assert row["event_types"], row["flag"]
            assert set(row["event_types"]) <= set(EVENT_CATALOG), row["flag"]
            assert row["group"] in GROUPS, row["flag"]
            assert row["severity"] in SEVERITIES, row["flag"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_sensor_aggregates_carry_their_members_and_the_strictest_severity(
        self, async_client: AsyncClient
    ):
        """One switch silences several events, so it must look as serious as the worst."""
        rows = {row["flag"]: row for row in (await async_client.get("/api/v1/notifications/events")).json()}

        assert rows["on_sensor_threshold"]["event_types"] == [
            "sensor_above_max",
            "sensor_below_min",
            "sensor_back_in_range",
        ]
        assert rows["on_sensor_threshold"]["severity"] == "error"
        assert rows["on_sensor_silent"]["event_types"] == ["sensor_silent", "sensor_speaking_again"]
        assert rows["on_sensor_silent"]["severity"] == "warning"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rows_arrive_grouped_in_catalog_order(self, async_client: AsyncClient):
        """The UI renders the list as it arrives, so the order is part of the contract."""
        from backend.app.services.notification_events import GROUPS

        rows = (await async_client.get("/api/v1/notifications/events")).json()

        seen: list[str] = []
        for row in rows:
            if not seen or seen[-1] != row["group"]:
                seen.append(row["group"])
        assert len(seen) == len(set(seen)), f"a group is split across the list: {seen}"
        assert seen == [g for g in GROUPS if g in seen]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_path_is_not_read_as_a_provider_id(self, async_client: AsyncClient):
        """``/events`` must be declared above ``/{provider_id}``; the other way it 422s."""
        response = await async_client.get("/api/v1/notifications/events")

        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestTelegramProviderRestarts:
    """Which provider edits may bounce the Telegram poller, and which may not (#50).

    A restart drops a healthy ``getUpdates`` long poll and builds a new bot;
    every save of a telegram provider used to do it, so renaming one raced a
    start against the poller it was replacing. The bot reads exactly one
    thing — ``telegram_bot.current_bot_token``, the OLDEST enabled telegram
    row's token — and the routes ask that reader before and after a save:
    only a different answer costs a restart. So a token on a disabled row, on
    a non-telegram row or on a younger enabled telegram row is as invisible
    as a rename, and creating a second enabled telegram provider behind the
    bot's own changes nothing either.
    """

    @pytest.fixture
    def restart_bot(self):
        """The attribute the routes' lazy import resolves at call time."""
        with patch("backend.app.services.telegram_bot.restart_telegram_bot", new_callable=AsyncMock) as mock:
            yield mock

    @staticmethod
    async def _telegram(notification_provider_factory, **kwargs):
        kwargs.setdefault("provider_type", "telegram")
        kwargs.setdefault("config", {"bot_token": "111:AAold"})
        return await notification_provider_factory(**kwargs)

    # ------------------------------------------------------------------
    # Update: only the token and the enabled flag matter
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_renaming_a_provider_leaves_the_poller_alone(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        provider = await self._telegram(notification_provider_factory, name="Farm bot")

        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"name": "Shop bot"})

        assert response.status_code == 200
        assert response.json()["name"] == "Shop bot"
        assert restart_bot.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_digest_schedule_is_not_the_bots_business(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        provider = await self._telegram(notification_provider_factory, daily_digest_enabled=False)

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"daily_digest_enabled": True, "daily_digest_time": "09:00"},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["daily_digest_enabled"] is True
        assert result["daily_digest_time"] == "09:00"
        assert restart_bot.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_config_key_beside_the_token_does_not_restart(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """The comparison is the token, not the config blob."""
        provider = await self._telegram(notification_provider_factory, config={"bot_token": "111:AAold"})

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"config": {"bot_token": "111:AAold", "chat_id": "42"}},
        )

        assert response.status_code == 200
        assert response.json()["config"] == {"bot_token": "111:AAold", "chat_id": "42"}
        assert restart_bot.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_new_token_restarts(self, async_client: AsyncClient, notification_provider_factory, restart_bot):
        provider = await self._telegram(notification_provider_factory, config={"bot_token": "111:AAold"})

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"config": {"bot_token": "222:AAnew"}},
        )

        assert response.status_code == 200
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_switching_the_provider_off_restarts(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """The bot is running on this row's token; it has to let go of it."""
        provider = await self._telegram(notification_provider_factory, enabled=True)

        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"enabled": False})

        assert response.status_code == 200
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_switching_the_provider_on_restarts(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """``current_bot_token`` only sees enabled rows, so this one just became the bot's."""
        provider = await self._telegram(notification_provider_factory, enabled=False)

        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"enabled": True})

        assert response.status_code == 200
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_editing_a_disabled_providers_token_leaves_the_poller_alone(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """A disabled row's token is invisible to ``current_bot_token``, so nothing the bot runs on moved.

        On an install that also carries an enabled telegram provider,
        bouncing the poller here would drop a healthy long poll for an edit
        the bot cannot see.
        """
        provider = await self._telegram(notification_provider_factory, enabled=False, config={"bot_token": "111:AAold"})

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"config": {"bot_token": "222:AAnew"}},
        )

        assert response.status_code == 200
        assert restart_bot.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retyping_a_telegram_provider_as_something_else_restarts(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """The bot was polling on this row; after the retype it is not a telegram row at all."""
        provider = await self._telegram(notification_provider_factory, enabled=True)

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"provider_type": "ntfy"},
        )

        assert response.status_code == 200
        assert response.json()["provider_type"] == "ntfy"
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retyping_another_provider_as_telegram_restarts(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """An enabled row that just became telegram is what ``current_bot_token`` now reads, there being no older one."""
        provider = await notification_provider_factory(
            provider_type="ntfy",
            enabled=True,
            config={"server": "https://ntfy.sh", "topic": "farm"},
        )

        response = await async_client.patch(
            f"/api/v1/notifications/{provider.id}",
            json={"provider_type": "telegram", "config": {"bot_token": "444:AAnew"}},
        )

        assert response.status_code == 200
        assert response.json()["provider_type"] == "telegram"
        assert restart_bot.await_count == 1

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_creating_an_enabled_provider_restarts(self, async_client: AsyncClient, restart_bot):
        response = await async_client.post(
            "/api/v1/notifications/",
            json={
                "name": "Farm bot",
                "provider_type": "telegram",
                "enabled": True,
                "config": {"bot_token": "111:AAold"},
            },
        )

        assert response.status_code == 200
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_creating_a_disabled_provider_does_not_restart(self, async_client: AsyncClient, restart_bot):
        """A row the token reader cannot see changes nothing about the poller."""
        response = await async_client.post(
            "/api/v1/notifications/",
            json={
                "name": "Spare bot",
                "provider_type": "telegram",
                "enabled": False,
                "config": {"bot_token": "333:AAspare"},
            },
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert restart_bot.await_count == 0

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleting_an_enabled_provider_restarts(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """The restart re-reads the next enabled provider, if there is one."""
        provider = await self._telegram(notification_provider_factory, enabled=True)

        response = await async_client.delete(f"/api/v1/notifications/{provider.id}")

        assert response.status_code == 200
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleting_a_disabled_provider_does_not_restart(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        provider = await self._telegram(notification_provider_factory, enabled=False)

        response = await async_client.delete(f"/api/v1/notifications/{provider.id}")

        assert response.status_code == 200
        assert restart_bot.await_count == 0

    # ------------------------------------------------------------------
    # Other provider types
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_non_telegram_provider_never_restarts(self, async_client: AsyncClient, restart_bot):
        """Create, rename, re-key, switch off and delete an ntfy row: no bot involved."""
        created = await async_client.post(
            "/api/v1/notifications/",
            json={
                "name": "Ntfy",
                "provider_type": "ntfy",
                "enabled": True,
                "config": {"server": "https://ntfy.sh", "topic": "farm"},
            },
        )
        assert created.status_code == 200
        provider_id = created.json()["id"]

        for payload in (
            {"name": "Ntfy renamed"},
            {"config": {"server": "https://ntfy.sh", "topic": "other"}},
            {"enabled": False},
        ):
            assert (await async_client.patch(f"/api/v1/notifications/{provider_id}", json=payload)).status_code == 200

        assert (await async_client.delete(f"/api/v1/notifications/{provider_id}")).status_code == 200
        assert restart_bot.await_count == 0

    # ------------------------------------------------------------------
    # More than one telegram row: the OLDEST enabled one is the bot
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_oldest_enabled_telegram_row_is_the_bot(
        self, async_client: AsyncClient, notification_provider_factory
    ):
        """``current_bot_token`` answers with the smallest enabled id, whatever else exists.

        Pins "oldest, not youngest" — reversing the reader's ORDER BY fails
        it. It cannot pin "ordered at all": on SQLite, where this suite runs,
        an unordered ``LIMIT 1`` over a table scan is rowid order anyway, so
        dropping the ORDER BY stays green here. The half of the clause with
        real value is PostgreSQL, where an UPDATE can move the row — that is
        the reader's docstring's argument, not this test's.

        ``async_client`` is requested for its side effect: it is the fixture
        that points the module-level session factory the reader opens at the
        test database.
        """
        from backend.app.services.telegram_bot import current_bot_token

        older = await self._telegram(notification_provider_factory, config={"bot_token": "111:AAolder"})
        younger = await self._telegram(notification_provider_factory, config={"bot_token": "222:AAyounger"})
        assert older.id < younger.id

        assert await current_bot_token() == "111:AAolder"

        with patch("backend.app.services.telegram_bot.restart_telegram_bot", new_callable=AsyncMock):
            assert (
                await async_client.patch(f"/api/v1/notifications/{older.id}", json={"enabled": False})
            ).status_code == 200
        assert await current_bot_token() == "222:AAyounger"

        with patch("backend.app.services.telegram_bot.restart_telegram_bot", new_callable=AsyncMock):
            assert (
                await async_client.patch(f"/api/v1/notifications/{older.id}", json={"enabled": True})
            ).status_code == 200
        assert await current_bot_token() == "111:AAolder"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_younger_enabled_telegram_row_never_costs_a_restart(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """Created, re-keyed and deleted behind the bot's row: the reader's answer never moves."""
        await self._telegram(notification_provider_factory, config={"bot_token": "111:AAbot"})

        created = await async_client.post(
            "/api/v1/notifications/",
            json={
                "name": "Second bot",
                "provider_type": "telegram",
                "enabled": True,
                "config": {"bot_token": "222:AAsecond"},
            },
        )
        assert created.status_code == 200
        second_id = created.json()["id"]
        assert restart_bot.await_count == 0

        response = await async_client.patch(
            f"/api/v1/notifications/{second_id}",
            json={"config": {"bot_token": "333:AAsecond-rekeyed"}},
        )
        assert response.status_code == 200
        assert restart_bot.await_count == 0

        assert (await async_client.delete(f"/api/v1/notifications/{second_id}")).status_code == 200
        assert restart_bot.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_removing_the_bots_row_hands_the_poller_to_the_next_one(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """Delete the oldest of two enabled rows: the reader now answers with the other's token."""
        bots = await self._telegram(notification_provider_factory, config={"bot_token": "111:AAbot"})
        await self._telegram(notification_provider_factory, config={"bot_token": "222:AAnext"})

        assert (await async_client.delete(f"/api/v1/notifications/{bots.id}")).status_code == 200
        assert restart_bot.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_switching_the_bots_row_off_and_on_moves_the_reader_twice(
        self, async_client: AsyncClient, notification_provider_factory, restart_bot
    ):
        """Off hands the poller to the younger row; on takes it back — one restart each."""
        bots = await self._telegram(notification_provider_factory, config={"bot_token": "111:AAbot"})
        await self._telegram(notification_provider_factory, config={"bot_token": "222:AAnext"})

        assert (
            await async_client.patch(f"/api/v1/notifications/{bots.id}", json={"enabled": False})
        ).status_code == 200
        assert restart_bot.await_count == 1

        assert (await async_client.patch(f"/api/v1/notifications/{bots.id}", json={"enabled": True})).status_code == 200
        assert restart_bot.await_count == 2
