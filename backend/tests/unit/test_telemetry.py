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


async def test_the_usage_counts_a_dispatch_error_apart_from_a_failed_print(db_session, printer_factory, monkeypatch):
    """Post-rebuild D-R7 / M3: telemetry asks the same outcome predicates as Statistics — an
    attempt that never started printing is a dispatch error, not a failed print.

    The clock and every ``created_at`` are fixed, so the counts do not depend on the hour the test
    runs at (the server default stamps the wall clock, and near midnight that is another day)."""
    from datetime import datetime, timezone

    from backend.app.models.archive import PrintArchive
    from backend.app.utils.failure_reasons import DISPATCH_NO_ACKNOWLEDGEMENT

    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setattr(telemetry, "_utc_now", lambda: datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(telemetry, "get_install_id", lambda: "install")
    monkeypatch.setattr(telemetry, "_ams_unit_count", lambda: 0)
    printer = await printer_factory()
    for status, reason in (
        ("completed", None),
        ("failed", None),  # a legacy physical failure with no class
        ("failed", "spaghetti"),
        ("cancelled", None),
        ("failed", DISPATCH_NO_ACKNOWLEDGEMENT),
        ("aborted", DISPATCH_NO_ACKNOWLEDGEMENT),
    ):
        db_session.add(
            PrintArchive(
                printer_id=printer.id,
                filename="p.3mf",
                print_name="p",
                file_path="",
                file_size=1,
                status=status,
                failure_reason=reason,
                created_at=datetime(2026, 10, 23, 8, 0),
            )
        )
    await db_session.commit()

    payload = await telemetry._build_payload(db_session)

    assert payload["usage"] == {"prints_completed": 1, "prints_failed": 3, "dispatch_errors": 2}


@pytest.mark.parametrize(
    "obico,octoeverywhere",
    [(None, None), ("true", "false"), ("false", "true"), ("true", None)],
)
async def test_the_features_report_each_failure_detector_by_its_own_flag(db_session, obico, octoeverywhere):
    """The cloud stores ``features.obico`` and ``features.octoeverywhere`` as two columns
    (bamdude-cloud Migration010). Each follows its own setting, and an unset one reads false."""
    from backend.app.models.settings import Settings

    for key, value in (("obico_enabled", obico), ("octoeverywhere_enabled", octoeverywhere)):
        if value is not None:
            db_session.add(Settings(key=key, value=value))
    await db_session.commit()

    features = await telemetry._features(db_session)

    assert features["obico"] is (obico == "true")
    assert features["octoeverywhere"] is (octoeverywhere == "true")
