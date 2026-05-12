"""Integration tests for /printers/{id}/settings GET + POST."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.printer_setting_audit import PrinterSettingAudit


def _client_with_state(printer_model="X1C", connected=True):
    c = MagicMock()
    c.state.connected = connected
    c.state.print_options = MagicMock()
    c.state.print_options.auto_recovery_step_loss = True
    c.state.print_options.sound_enable = None
    c.state.print_options.filament_tangle_detect = None
    c.state.print_options.nozzle_blob_detect = None
    c.state.print_options.air_purification = None
    c.state.print_options.open_door_check = None
    c.state.print_options.save_remote_to_storage = None
    c.state.print_options.plate_type_detect = None
    c.state.print_options.plate_align_check = None
    c.state.print_options.snapshot_enabled = None
    c.state.print_options.fod_check = None
    c.state.print_options.displacement_detection = None
    c.state.print_options.spaghetti_detector = True
    c.state.print_options.halt_print_sensitivity = "medium"
    c.state.print_options.nozzle_clumping_detector = False
    c.state.print_options.nozzle_clumping_sensitivity = "medium"
    c.state.print_options.pileup_detector = False
    c.state.print_options.pileup_sensitivity = "medium"
    c.state.print_options.airprint_detector = False
    c.state.print_options.airprint_sensitivity = "medium"
    c.state.print_options.first_layer_inspector = False
    c.state.print_options.printing_monitor = False
    c.state.nozzles = []
    c.module_vers = {}
    return c


@pytest.mark.asyncio
async def test_get_404_when_not_online(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = None
        r = await async_client.get(f"/api/v1/printers/{printer.id}/settings")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_returns_state_and_supports(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = _client_with_state("X1C")
        r = await async_client.get(f"/api/v1/printers/{printer.id}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["print_options"]["auto_recovery"] is True
    assert body["supports"]["spaghetti_detector"] is True
    assert body["supports"]["parts_dual"] is False


@pytest.mark.asyncio
async def test_post_bool_action_publishes_and_audits(async_client, printer_factory, db_session):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    client.print_option_auto_recovery = MagicMock(return_value=(True, "SEQ-PS-1"))

    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={"action": "print_option_bool", "key": "auto_recovery", "enabled": False},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sequence_id"] == "SEQ-PS-1"
    client.print_option_auto_recovery.assert_called_once_with(False)

    rows = (
        (await db_session.execute(select(PrinterSettingAudit).where(PrinterSettingAudit.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].action == "print_option_bool"
    assert rows[0].tab == "print_options"
    assert rows[0].result == "sent"


@pytest.mark.asyncio
async def test_post_xcam_control_with_sensitivity(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    client.xcam_control_for_settings = MagicMock(return_value=(True, "SEQ-PS-2"))
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={
                "action": "xcam_control",
                "module": "spaghetti_detector",
                "enabled": True,
                "sensitivity": "high",
            },
        )
    assert r.status_code == 200
    client.xcam_control_for_settings.assert_called_once_with("spaghetti_detector", enabled=True, sensitivity="high")


@pytest.mark.asyncio
async def test_post_unsupported_returns_409(async_client, printer_factory):
    printer = await printer_factory(model="A1 Mini")
    client = _client_with_state("A1 Mini")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={
                "action": "xcam_control",
                "module": "spaghetti_detector",
                "enabled": True,
                "sensitivity": "medium",
            },
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_set_nozzle_returns_409_parts_not_editable(async_client, printer_factory):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={
                "action": "set_nozzle",
                "nozzle_id": 0,
                "type": "stainless_steel",
                "diameter": 0.4,
                "flow_type": "standard",
            },
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_post_mqtt_failure_returns_504_and_audits_error(async_client, printer_factory, db_session):
    printer = await printer_factory(model="X1C")
    client = _client_with_state("X1C")
    client.print_option_auto_recovery = MagicMock(return_value=(False, None))
    with patch("backend.app.api.routes.printer_settings.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/settings",
            json={"action": "print_option_bool", "key": "auto_recovery", "enabled": True},
        )
    assert r.status_code == 504
    rows = (
        (await db_session.execute(select(PrinterSettingAudit).where(PrinterSettingAudit.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].result == "error"
