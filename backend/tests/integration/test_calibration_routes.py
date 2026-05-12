"""Integration tests for Filament Calibration REST routes (m062 / Plan 1)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.calibration_audit import CalibrationAudit
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.services.bambu_mqtt import ExtrusionCaliResult, PACalibHistoryEntry


def _mock_client(*, online: bool = True, pa_auto: bool = True):
    c = MagicMock()
    c.state.connected = online
    c.state.is_support_pa_calibration = pa_auto
    c.state.is_support_auto_flow_calibration = pa_auto
    c.state.nozzles = []
    c.state.extrusion_cali_results = []
    c.state.extrusion_cali_history = []
    c.extrusion_cali_start = MagicMock(return_value=(True, "SEQ-1"))
    c.flow_rate_cali_start = MagicMock(return_value=(True, "SEQ-2"))
    c.extrusion_cali_set = MagicMock(return_value=True)
    c.extrusion_cali_sel = MagicMock(return_value=True)
    c.extrusion_cali_query_history = MagicMock(return_value=(True, "SEQ-Q"))
    c.stop_print = MagicMock(return_value=True)
    return c


@pytest.mark.asyncio
async def test_get_capabilities_x1c(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = _mock_client()
        r = await async_client.get(f"/api/v1/printers/{p.id}/calibration/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pa_auto"] is True
    assert body["pa_manual"] is True


@pytest.mark.asyncio
async def test_get_capabilities_offline_returns_404(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = None
        r = await async_client.get(f"/api/v1/printers/{p.id}/calibration/capabilities")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_session_auto(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    with (
        patch("backend.app.api.routes.filament_calibration.printer_manager") as pm,
        patch("backend.app.services.calibration_service.printer_manager") as pm2,
    ):
        pm.get_client.return_value = _mock_client()
        pm2.get_client.return_value = _mock_client()
        r = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/sessions",
            json={
                "cali_mode": "auto_pa_line",
                "method": "auto",
                "nozzle_diameter": 0.4,
                "nozzle_volume_type": "standard",
                "extruder_id": 0,
                "filaments": [
                    {
                        "ams_id": 0,
                        "slot_id": 0,
                        "tray_id": 0,
                        "filament_id": "GFG00",
                        "filament_setting_id": "GFG00_60@BBL",
                        "bed_temp": 60,
                        "nozzle_temp": 220,
                        "max_volumetric_speed": 12.0,
                    }
                ],
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "running"
    assert body["method"] == "auto"


@pytest.mark.asyncio
async def test_post_session_concurrent_409(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    body = {
        "cali_mode": "auto_pa_line",
        "method": "auto",
        "nozzle_diameter": 0.4,
        "nozzle_volume_type": "standard",
        "extruder_id": 0,
        "filaments": [
            {
                "ams_id": 0,
                "slot_id": 0,
                "tray_id": 0,
                "filament_id": "GFG00",
                "filament_setting_id": "GFG00_60@BBL",
                "bed_temp": 60,
                "nozzle_temp": 220,
                "max_volumetric_speed": 12.0,
            }
        ],
    }
    with (
        patch("backend.app.api.routes.filament_calibration.printer_manager") as pm,
        patch("backend.app.services.calibration_service.printer_manager") as pm2,
    ):
        pm.get_client.return_value = _mock_client()
        pm2.get_client.return_value = _mock_client()
        r1 = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/sessions",
            json=body,
        )
        assert r1.status_code == 200, r1.text
        r2 = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/sessions",
            json=body,
        )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_post_manual_result_pa(async_client, printer_factory, db_session):
    p = await printer_factory(model="P1S")
    s = CalibrationSession(
        printer_id=p.id,
        user_id=None,
        cali_mode="pa_line",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json=json.dumps(
            [
                {
                    "ams_id": 0,
                    "slot_id": 0,
                    "tray_id": 0,
                    "filament_id": "GFG00",
                    "setting_id": "GFG00_60@BBL",
                    "nozzle_id": "HS20",
                    "nozzle_diameter": "0.4",
                    "nozzle_temp": 220,
                }
            ]
        ),
        status="awaiting_user_input",
        stage=1,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    with (
        patch("backend.app.api.routes.filament_calibration.printer_manager") as pm,
        patch("backend.app.services.calibration_service.printer_manager") as pm2,
    ):
        pm.get_client.return_value = _mock_client()
        pm2.get_client.return_value = _mock_client()
        r = await async_client.post(
            f"/api/v1/calibration/sessions/{s.id}/manual-result",
            json={"best_line_index": 24},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["saved_rows"]) == 1
    assert abs(body["saved_rows"][0]["pa_k_value"] - 0.048) < 1e-9


@pytest.mark.asyncio
async def test_post_auto_result(async_client, printer_factory, db_session):
    p = await printer_factory(model="X1C")
    s = CalibrationSession(
        printer_id=p.id,
        user_id=None,
        cali_mode="auto_pa_line",
        method="auto",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json=json.dumps(
            [
                {
                    "ams_id": 0,
                    "slot_id": 0,
                    "tray_id": 0,
                    "filament_id": "GFG00",
                    "setting_id": "GFG00_60@BBL",
                    "nozzle_id": "HS20",
                    "nozzle_diameter": "0.4",
                }
            ]
        ),
        status="awaiting_user_input",
        stage=1,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    client = _mock_client()
    client.state.extrusion_cali_results = [
        ExtrusionCaliResult(
            tray_id=0,
            ams_id=0,
            slot_id=0,
            extruder_id=0,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            filament_id="GFG00",
            setting_id="GFG00_60@BBL",
            k_value=0.05,
            n_coef=1.0,
            confidence=0,
        )
    ]
    with (
        patch("backend.app.api.routes.filament_calibration.printer_manager") as pm,
        patch("backend.app.services.calibration_service.printer_manager") as pm2,
    ):
        pm.get_client.return_value = client
        pm2.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/calibration/sessions/{s.id}/auto-result",
            json={
                "results": [
                    {"tray_id": 0, "save": True, "k_value": 0.05, "name": "PLA — PA 0.05"},
                ]
            },
        )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_list_filament_calibrations(async_client, printer_factory, db_session):
    await printer_factory(model="P1S")
    db_session.add(
        FilamentCalibration(
            printer_model="P1S",
            filament_id="GFG00",
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            pa_k_value=0.048,
            cali_mode="pa_line",
            source="manual",
            is_active=True,
            cali_idx=3,
            name="row1",
        )
    )
    await db_session.commit()

    r = await async_client.get("/api/v1/filament-calibrations?printer_model=P1S")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_set_active_flips_others(async_client, printer_factory, db_session):
    await printer_factory(model="P1S")
    r1 = FilamentCalibration(
        printer_model="P1S",
        filament_id="GFG00",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.04,
        cali_mode="pa_line",
        source="manual",
        is_active=True,
        cali_idx=1,
        name="r1",
    )
    r2 = FilamentCalibration(
        printer_model="P1S",
        filament_id="GFG00",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.05,
        cali_mode="pa_line",
        source="manual",
        is_active=False,
        cali_idx=2,
        name="r2",
    )
    db_session.add_all([r1, r2])
    await db_session.commit()
    await db_session.refresh(r2)

    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = _mock_client()
        resp = await async_client.post(f"/api/v1/filament-calibrations/{r2.id}/set-active")
    assert resp.status_code == 200
    await db_session.refresh(r1)
    await db_session.refresh(r2)
    assert r1.is_active is False
    assert r2.is_active is True


@pytest.mark.asyncio
async def test_delete_calibration(async_client, printer_factory, db_session):
    await printer_factory(model="P1S")
    row = FilamentCalibration(
        printer_model="P1S",
        filament_id="GFG00",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.04,
        cali_mode="pa_line",
        source="manual",
        is_active=True,
        cali_idx=1,
        name="r",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    r = await async_client.delete(f"/api/v1/filament-calibrations/{row.id}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_get_history_returns_state_entries(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    client = _mock_client()
    client.state.extrusion_cali_history = [
        PACalibHistoryEntry(
            cali_idx=0,
            name="r1",
            filament_id="GFG00",
            setting_id="GFG00_60@BBL",
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            k_value=0.04,
            n_coef=1.0,
        )
    ]
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.get(f"/api/v1/printers/{p.id}/calibration/history")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_history_refresh_triggers_mqtt_get(async_client, printer_factory):
    p = await printer_factory(model="X1C")
    client = _mock_client()
    with patch("backend.app.api.routes.filament_calibration.printer_manager") as pm:
        pm.get_client.return_value = client
        r = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/history/refresh?nozzle_diameter=0.4",
        )
    assert r.status_code == 202
    client.extrusion_cali_query_history.assert_called_once()


@pytest.mark.asyncio
async def test_list_awaiting_sessions(async_client, printer_factory, db_session):
    p = await printer_factory(model="P1S")
    s = CalibrationSession(
        printer_id=p.id,
        user_id=None,
        cali_mode="pa_line",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json="[]",
        status="awaiting_user_input",
    )
    db_session.add(s)
    await db_session.commit()

    r = await async_client.get(f"/api/v1/calibration/sessions?printer_id={p.id}&status=awaiting_user_input")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_audit_row_written_on_start_session(async_client, printer_factory, db_session):
    p = await printer_factory(model="X1C")
    with (
        patch("backend.app.api.routes.filament_calibration.printer_manager") as pm,
        patch("backend.app.services.calibration_service.printer_manager") as pm2,
    ):
        pm.get_client.return_value = _mock_client()
        pm2.get_client.return_value = _mock_client()
        r = await async_client.post(
            f"/api/v1/printers/{p.id}/calibration/sessions",
            json={
                "cali_mode": "auto_pa_line",
                "method": "auto",
                "nozzle_diameter": 0.4,
                "nozzle_volume_type": "standard",
                "extruder_id": 0,
                "filaments": [
                    {
                        "ams_id": 0,
                        "slot_id": 0,
                        "tray_id": 0,
                        "filament_id": "GFG00",
                        "filament_setting_id": "GFG00_60@BBL",
                        "bed_temp": 60,
                        "nozzle_temp": 220,
                        "max_volumetric_speed": 12.0,
                    }
                ],
            },
        )
    assert r.status_code == 200, r.text
    rows = (
        (await db_session.execute(select(CalibrationAudit).where(CalibrationAudit.printer_id == p.id))).scalars().all()
    )
    assert any(row.action == "start_session" and row.result == "ok" for row in rows)
