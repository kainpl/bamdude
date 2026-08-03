"""Who receives a sensor alert, and in whose words."""

import pytest


async def _provider(db_session, **kwargs):
    import json

    from backend.app.models.notification import NotificationProvider

    row = NotificationProvider(
        name=kwargs.pop("name", "ntfy"),
        provider_type="ntfy",
        enabled=True,
        # The column stores JSON as text; a dict reaches sqlite3 unbindable.
        config=json.dumps({"topic": "bamdude", "server_url": "https://ntfy.sh"}),
        **kwargs,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_scoped_provider_does_not_receive_sensor_alerts(db_session):
    """A sensor is not a printer. Somebody who bound a provider to Printer 3
    asked for that printer's news."""
    from backend.app.services.notification_service import NotificationService

    await _provider(db_session, name="bound", on_sensor_threshold=True, printer_id=1)

    service = NotificationService()
    providers = await service._get_providers_for_event(db_session, "on_sensor_threshold", unscoped_only=True)

    assert providers == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unbound_provider_does_receive_them(db_session):
    from backend.app.services.notification_service import NotificationService

    await _provider(db_session, name="free", on_sensor_threshold=True)

    service = NotificationService()
    providers = await service._get_providers_for_event(db_session, "on_sensor_threshold", unscoped_only=True)

    assert [p.name for p in providers] == ["free"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_silence_toggle_is_separate_from_the_threshold_one(db_session):
    """ "Tell me about the room" and "tell me about the device" are the two
    questions a person actually separates."""
    from backend.app.services.notification_service import NotificationService

    await _provider(db_session, name="room-only", on_sensor_threshold=True, on_sensor_silent=False)

    service = NotificationService()
    silent = await service._get_providers_for_event(db_session, "on_sensor_silent", unscoped_only=True)

    assert silent == []


def test_the_quantity_name_is_translated():
    """A template is seeded once in the system language; substituting the
    English key into a Ukrainian sentence puts an English word inside it."""
    from backend.app.i18n import t

    assert t("uk", "measurements", "temperature") == "температура"
    assert t("en", "measurements", "temperature") == "temperature"


def test_every_registry_quantity_has_a_name_in_both_locales():
    """A quantity added to the registry without a name here would appear in a
    message as its own key.

    The files are read directly rather than asked through ``t()``: a missing
    key returns the key itself, and several English names ARE their key
    ("temperature"), so a comparison could not tell the two apart.
    """
    import json
    from pathlib import Path

    from backend.app.services.zigbee.measurements import BY_KEY

    data_dir = Path(__file__).resolve().parents[2] / "app" / "data"
    for lang in ("en", "uk"):
        names = json.loads((data_dir / f"measurements_{lang}.json").read_text(encoding="utf-8"))
        assert set(BY_KEY) <= set(names), (lang, set(BY_KEY) - set(names))


def test_force_immediate_is_gone():
    """It was declared and never read: six call sites believed they bypassed
    the digest and did not. Left in place, the next one copies it."""
    import inspect

    from backend.app.services.notification_service import NotificationService

    signature = inspect.signature(NotificationService._send_to_providers)
    assert "force_immediate" not in signature.parameters
