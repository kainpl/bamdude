"""Starting, stopping and resuming MQTT recording.

⚠️ The resume case IS the feature. Recording exists so a capture outlives the
window that started it; a backend restart that silently stopped it would be the
same complaint in a longer form.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

MOD = "backend.app.services.mqtt_recorder"


def _point_at_the_test_database(monkeypatch, test_engine):
    """``resume_recordings`` opens its own session, so it must be pointed at the
    test database — otherwise it queries the real one and finds no column."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(
        "backend.app.core.database.async_session",
        async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
    )


async def test_enabling_it_persists_the_intent(async_client, printer_factory, db_session):
    printer = await printer_factory()
    with patch(f"{MOD}.mqtt_recorder.start", MagicMock()):
        resp = await async_client.post(f"/api/v1/printers/{printer.id}/mqtt-recording", json={"enabled": True})

    assert resp.status_code == 200
    await db_session.refresh(printer)
    assert printer.mqtt_recording is True
    assert printer.mqtt_recording_started_at is not None


async def test_disabling_it_clears_the_intent(async_client, printer_factory, db_session):
    printer = await printer_factory()
    with (
        patch(f"{MOD}.mqtt_recorder.start", MagicMock()),
        patch(f"{MOD}.mqtt_recorder.stop", MagicMock()) as stop,
    ):
        await async_client.post(f"/api/v1/printers/{printer.id}/mqtt-recording", json={"enabled": True})
        resp = await async_client.post(f"/api/v1/printers/{printer.id}/mqtt-recording", json={"enabled": False})

    assert resp.status_code == 200
    stop.assert_called_once_with(printer.id)
    await db_session.refresh(printer)
    assert printer.mqtt_recording is False


async def test_a_printer_with_no_live_client_is_refused_not_silently_ignored(async_client, printer_factory, db_session):
    """409, so the UI can say why the switch did not take. A silent success
    would leave a badge claiming a recording that does not exist."""
    printer = await printer_factory()
    with patch(f"{MOD}.mqtt_recorder.start", MagicMock(side_effect=RuntimeError("no live MQTT client"))):
        resp = await async_client.post(f"/api/v1/printers/{printer.id}/mqtt-recording", json={"enabled": True})

    assert resp.status_code == 409
    await db_session.refresh(printer)
    assert printer.mqtt_recording is False, "the intent must not persist when the recording never started"


async def test_a_restart_resumes_every_recording_it_finds(db_session, printer_factory, test_engine, monkeypatch):
    from backend.app.services.mqtt_recorder import resume_recordings

    _point_at_the_test_database(monkeypatch, test_engine)

    printer = await printer_factory()
    printer.mqtt_recording = True
    await db_session.commit()

    started = MagicMock()
    with patch(f"{MOD}.mqtt_recorder.start", started):
        await resume_recordings()

    started.assert_called_once_with(printer.id)


async def test_resume_skips_printers_that_were_never_recording(db_session, printer_factory, test_engine, monkeypatch):
    from backend.app.services.mqtt_recorder import resume_recordings

    _point_at_the_test_database(monkeypatch, test_engine)

    await printer_factory()

    started = MagicMock()
    with patch(f"{MOD}.mqtt_recorder.start", started):
        await resume_recordings()

    started.assert_not_called()


async def test_a_printer_that_cannot_be_resumed_does_not_stop_the_rest(
    db_session, printer_factory, test_engine, monkeypatch
):
    """One offline printer must not cost the others their recording — this runs
    on the startup path and inside the watchdog sweep."""
    from backend.app.services.mqtt_recorder import resume_recordings

    _point_at_the_test_database(monkeypatch, test_engine)

    first = await printer_factory(name="A", serial_number="S1", ip_address="192.168.1.51")
    second = await printer_factory(name="B", serial_number="S2", ip_address="192.168.1.52")
    first.mqtt_recording = True
    second.mqtt_recording = True
    await db_session.commit()

    started = MagicMock(side_effect=[RuntimeError("no live MQTT client"), None])
    with patch(f"{MOD}.mqtt_recorder.start", started):
        await resume_recordings()

    assert started.call_count == 2
