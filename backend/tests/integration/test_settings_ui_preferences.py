"""Tests for the curated GET /settings/ui-preferences endpoint (#1293).

The endpoint exists so pages like Printers render correctly for non-admin
operators who lack SETTINGS_READ. It must expose ONLY the whitelisted
non-sensitive UI fields — never a credential or any other setting.
"""

import pytest
from httpx import AsyncClient

from backend.app.api.routes.settings import _UI_PREFERENCE_FIELDS


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ui_preferences_returns_exactly_curated_fields(async_client: AsyncClient):
    """Response keys are exactly the whitelist — guards the dict comprehension
    from drifting to a broader passthrough."""
    resp = await async_client.get("/api/v1/settings/ui-preferences")
    assert resp.status_code == 200
    assert set(resp.json().keys()) == set(_UI_PREFERENCE_FIELDS)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ui_preferences_exposes_a_known_ui_field(async_client: AsyncClient):
    """A representative UI field comes back with a usable value (not 403/empty)."""
    resp = await async_client.get("/api/v1/settings/ui-preferences")
    assert resp.status_code == 200
    body = resp.json()
    assert "camera_view_mode" in body
    assert "time_format" in body


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ui_preferences_does_not_leak_sensitive_settings(async_client: AsyncClient, db_session):
    """Leak canary: seed sensitive settings with recognizable values and assert
    neither their keys nor their values appear anywhere in the response."""
    from backend.app.models.settings import Settings

    canaries = {
        "mqtt_password": "CANARY_mqtt_pw_8f3a",
        "smtp_password": "CANARY_smtp_pw_8f3a",
        "ldap_bind_password": "CANARY_ldap_pw_8f3a",
        "ha_access_token": "CANARY_ha_token_8f3a",
        "oidc_client_secret": "CANARY_oidc_secret_8f3a",
    }
    for key, value in canaries.items():
        db_session.add(Settings(key=key, value=value))
    await db_session.commit()

    resp = await async_client.get("/api/v1/settings/ui-preferences")
    assert resp.status_code == 200
    keys = set(resp.json().keys())
    raw_body = resp.text
    for key, value in canaries.items():
        assert key not in keys, f"sensitive key {key} leaked into ui-preferences"
        assert value not in raw_body, f"sensitive value for {key} leaked into ui-preferences"
