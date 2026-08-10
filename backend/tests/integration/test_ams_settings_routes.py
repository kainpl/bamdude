"""Integration tests for /printers/{id}/ams/settings GET + POST."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.ams_setting_audit import AmsSettingAudit


def _state(connected=True, **overrides):
    """Build a minimal state object stub used by the router."""
    s = MagicMock()
    s.connected = connected
    s.ams_insertion_update = None
    s.ams_power_on_update = None
    s.ams_remain_capacity = None
    s.ams_auto_switch_filament = None
    s.ams_air_print_detect = None
    # MagicMock would answer truthy for these, and ``firmware_switch`` is now
    # ``bool(state.ams_firmwares)`` — an unset attribute would turn the switch
    # ON for every printer in every test.
    s.ams_firmwares = []
    s.ams_firmware_idx_run = None
    s.ams_firmware_idx_sel = None
    s.ams_firmware_status = None
    # Same reason as the block above, and a sharper one: several AMS rows are now
    # answered from the model's mirrored config, whose blocks are layered by
    # firmware version. A MagicMock here is not a version string, so every lookup
    # would fall back to the 2023 base block — which understates the X1 family
    # exactly where these tests assert it is supported.
    s.firmware_version = "01.08.00.00"
    s.print_option_support = {}
    s.firmware_upgrade_status = None
    s.ext_has_filament = {}
    s.state = "IDLE"
    s.raw_data = {}
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _client_with_state(state):
    c = MagicMock()
    c.state = state
    return c


@pytest.mark.asyncio
async def test_get_returns_state_and_supports(async_client, printer_factory, db_session):
    printer = await printer_factory(model="X1C")
    client = _client_with_state(
        _state(
            ams_insertion_update=True,
            ams_power_on_update=False,
            ams_auto_switch_filament=True,
        )
    )
    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.get(f"/api/v1/printers/{printer.id}/ams/settings")

    assert r.status_code == 200
    body = r.json()
    assert body["state"]["insertion_update"] is True
    assert body["state"]["power_on_update"] is False
    assert body["state"]["auto_switch_filament"] is True
    # X1C has RFID-capable AMS but NO air_print here.
    assert body["supports"]["insertion_update"] is True
    assert body["supports"]["air_print_detect"] is False
    assert body["supports"]["firmware_switch"] is False


@pytest.mark.asyncio
async def test_get_404_when_printer_not_found(async_client):
    r = await async_client.get("/api/v1/printers/99999/ams/settings")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_404_when_offline(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = None
        r = await async_client.get(f"/api/v1/printers/{printer.id}/ams/settings")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_user_setting_publishes_and_audits(async_client, printer_factory, db_session):
    printer = await printer_factory(model="X1C")
    client = _client_with_state(_state())
    client.ams_user_setting = MagicMock(return_value=(True, "SEQ-1"))

    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            json={
                "action": "user_setting",
                "startup_read_option": True,
                "tray_read_option": False,
                "calibrate_remain_flag": True,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sequence_id"] == "SEQ-1"

    client.ams_user_setting.assert_called_once_with(startup_read=True, tray_read=False, calibrate_remain=True)

    rows = (
        (await db_session.execute(select(AmsSettingAudit).where(AmsSettingAudit.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].action == "user_setting"
    assert rows[0].result == "sent"
    assert rows[0].sequence_id == "SEQ-1"


@pytest.mark.asyncio
async def test_post_calibrate_sends_gcode(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    client = _client_with_state(_state())
    client.ams_calibrate = MagicMock(return_value=True)
    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            json={"action": "calibrate", "ams_id": 0},
        )
    assert r.status_code == 200
    client.ams_calibrate.assert_called_once_with(0)


@pytest.mark.asyncio
async def test_post_reorder_no_payload_calls_ams_reset_sequence(async_client, printer_factory):
    printer = await printer_factory(model="H2D")
    client = _client_with_state(_state())
    client.ams_reset_sequence = MagicMock(return_value=(True, "SEQ-2"))
    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            json={"action": "reorder"},
        )
    assert r.status_code == 200
    client.ams_reset_sequence.assert_called_once_with()


@pytest.mark.asyncio
async def test_post_unsupported_returns_409(async_client, printer_factory, db_session):
    """A printer that offered no firmware list cannot be switched.

    The gate used to be the model ("only a full A1"); it is now the device's own
    answer — ``ams_firmwares`` empty means the printer never offered a switch.
    The model here is incidental.
    """
    printer = await printer_factory(model="A1 Mini")
    client = _client_with_state(_state())
    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            json={"action": "firmware_switch", "firmware_idx": 1},
        )
    assert r.status_code == 409
    # No audit row on 409 (nothing was sent).
    rows = (
        (await db_session.execute(select(AmsSettingAudit).where(AmsSettingAudit.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_post_mqtt_publish_failure_returns_504_and_audits_error(async_client, printer_factory, db_session):
    printer = await printer_factory(model="X1C")
    client = _client_with_state(_state())
    client.print_option_auto_switch_filament = MagicMock(return_value=(False, None))
    with patch("backend.app.api.routes.ams_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/ams/settings",
            json={"action": "auto_switch_filament", "enabled": True},
        )
    assert r.status_code == 504
    rows = (
        (await db_session.execute(select(AmsSettingAudit).where(AmsSettingAudit.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].result == "error"


@pytest.mark.asyncio
async def test_post_requires_auth(async_client, printer_factory):
    """Strip Authorization header and confirm 401."""
    printer = await printer_factory(model="X1C")
    r = await async_client.post(
        f"/api/v1/printers/{printer.id}/ams/settings",
        json={"action": "auto_switch_filament", "enabled": True},
        headers={"Authorization": ""},
    )
    assert r.status_code in (401, 403)
