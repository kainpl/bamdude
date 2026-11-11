"""HA runtime reads one coherent DB tuple with independent environment overrides."""

import pytest

from backend.app.api.routes.settings import get_homeassistant_settings, set_setting


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url_env", "token_env", "db_enabled", "enabled"),
    [
        (None, None, True, True),
        ("http://supervisor/core", "env-token", False, True),
        ("http://supervisor/core", None, False, False),
        (None, "env-token", False, False),
        ("", "", True, True),
        ("http://supervisor/core", "env-token", True, True),
        (None, None, False, False),
    ],
)
async def test_environment_overrides_keep_runtime_secret_private(
    db_session, monkeypatch, url_env, token_env, db_enabled, enabled
):
    for key, value in {
        "ha_url": "http://db-url:8123",
        "ha_token": "db-token",
        "ha_enabled": str(db_enabled).lower(),
    }.items():
        await set_setting(db_session, key, value)
    await db_session.commit()
    for key, value in {"HA_URL": url_env, "HA_TOKEN": token_env}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    result = await get_homeassistant_settings(db_session)
    assert result == {
        "ha_url": url_env or "http://db-url:8123",
        "ha_token": token_env or "db-token",
        "ha_enabled": enabled,
        "ha_url_from_env": bool(url_env),
        "ha_token_from_env": bool(token_env),
        "ha_env_managed": bool(url_env and token_env),
    }
