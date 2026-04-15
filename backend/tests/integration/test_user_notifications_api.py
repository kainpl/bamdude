"""Integration tests for User Notifications API endpoints.

Tests the full request/response cycle for /api/v1/user-notifications/ endpoints.
"""

import pytest
from httpx import AsyncClient


class TestUserNotificationsAPI:
    """Integration tests for /api/v1/user-notifications/ endpoints."""

    # ========================================================================
    # GET /preferences — no auth
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_preferences_returns_defaults_when_no_auth(self, async_client: AsyncClient):
        """Without auth, GET should return all-enabled defaults."""
        response = await async_client.get("/api/v1/user-notifications/preferences")

        assert response.status_code == 200
        data = response.json()
        assert data["notify_print_start"] is True
        assert data["notify_print_complete"] is True
        assert data["notify_print_failed"] is True
        assert data["notify_print_stopped"] is True

    # ========================================================================
    # PUT /preferences — no auth
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_preferences_fails_without_auth(self, async_client: AsyncClient):
        """Without a bearer token, PUT should return 401 (auth is always on)."""
        data = {
            "notify_print_start": False,
            "notify_print_complete": True,
            "notify_print_failed": True,
            "notify_print_stopped": False,
        }

        # async_client sends a default admin token — explicitly clear it to
        # exercise the no-token path now that auth is always enabled.
        response = await async_client.put(
            "/api/v1/user-notifications/preferences",
            json=data,
            headers={"Authorization": ""},
        )

        assert response.status_code == 401

    # ========================================================================
    # Schema validation
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_preferences_rejects_missing_fields(self, async_client: AsyncClient):
        """PUT should reject requests missing required boolean fields."""
        data = {
            "notify_print_start": True,
            # missing other fields
        }

        response = await async_client.put("/api/v1/user-notifications/preferences", json=data)

        assert response.status_code == 422  # Pydantic validation error

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_preferences_rejects_invalid_type(self, async_client: AsyncClient):
        """PUT should reject values that cannot be coerced to boolean."""
        data = {
            "notify_print_start": [1, 2, 3],
            "notify_print_complete": True,
            "notify_print_failed": True,
            "notify_print_stopped": True,
        }

        response = await async_client.put("/api/v1/user-notifications/preferences", json=data)

        assert response.status_code == 422
