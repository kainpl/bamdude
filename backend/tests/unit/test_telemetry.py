"""Unit tests for the opt-out telemetry sender's gating logic."""

import pytest

from backend.app.services import telemetry


class _DummyDB:
    """Stand-in DB; _is_enabled only forwards it to a patched get_setting."""


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("", False), ("true", True), ("True", True), ("1", True), ("yes", True), ("false", False)],
)
def test_truthy(value, expected):
    assert telemetry._truthy(value) is expected


@pytest.mark.asyncio
async def test_is_enabled_default_on(monkeypatch):
    """Opt-out: with no stored setting, telemetry is ON by default."""
    monkeypatch.setattr(telemetry, "TELEMETRY_DISABLED", False)
    monkeypatch.setattr(telemetry, "TELEMETRY_RELAY_URL", "https://example.test/api/telemetry")

    async def fake_get_setting(_db, _key):
        return None

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    assert await telemetry._is_enabled(_DummyDB()) is True


@pytest.mark.asyncio
async def test_is_enabled_explicit_off(monkeypatch):
    monkeypatch.setattr(telemetry, "TELEMETRY_DISABLED", False)
    monkeypatch.setattr(telemetry, "TELEMETRY_RELAY_URL", "https://example.test/api/telemetry")

    async def fake_get_setting(_db, _key):
        return "false"

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    assert await telemetry._is_enabled(_DummyDB()) is False


@pytest.mark.asyncio
async def test_is_enabled_env_kill_switch(monkeypatch):
    monkeypatch.setattr(telemetry, "TELEMETRY_DISABLED", True)
    monkeypatch.setattr(telemetry, "TELEMETRY_RELAY_URL", "https://example.test/api/telemetry")
    assert await telemetry._is_enabled(_DummyDB()) is False


@pytest.mark.asyncio
async def test_is_enabled_no_url(monkeypatch):
    monkeypatch.setattr(telemetry, "TELEMETRY_DISABLED", False)
    monkeypatch.setattr(telemetry, "TELEMETRY_RELAY_URL", "")
    assert await telemetry._is_enabled(_DummyDB()) is False


@pytest.mark.asyncio
async def test_build_payload_noops_without_install_id(monkeypatch):
    monkeypatch.setattr(telemetry, "get_install_id", lambda: None)
    assert await telemetry._build_payload(_DummyDB()) is None


class _FakeSessionCM:
    async def __aenter__(self):
        return _DummyDB()

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_send_once_skips_when_disabled(monkeypatch):
    """A disabled install must not build a payload or hit the network."""
    monkeypatch.setattr(telemetry, "TELEMETRY_DISABLED", True)
    monkeypatch.setattr(telemetry, "async_session", lambda: _FakeSessionCM())

    called = {"build": False}

    async def fake_build(_db):
        called["build"] = True
        return {}

    monkeypatch.setattr(telemetry, "_build_payload", fake_build)
    assert await telemetry.send_telemetry_once() is False
    assert called["build"] is False
