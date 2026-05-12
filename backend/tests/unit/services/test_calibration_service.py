"""Tests CalibrationService — start/submit/save/cancel orchestration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.calibration_session import CalibrationSession
from backend.app.services.bambu_mqtt import ExtrusionCaliResult
from backend.app.services.calibration_constants import CaliMethod, CaliMode
from backend.app.services.calibration_service import (
    CalibFilamentInput,
    CalibrationService,
    resolve_active_calibration,
    resolve_asset_path,
)


@pytest.fixture
def service():
    return CalibrationService()


@pytest.fixture
def mock_client():
    c = MagicMock()
    c.state.connected = True
    c.state.is_support_pa_calibration = True
    c.state.is_support_auto_flow_calibration = True
    c.state.extrusion_cali_status = "idle"
    c.state.extrusion_cali_results = []
    c.extrusion_cali_start = MagicMock(return_value=(True, "SEQ-CALI-1"))
    c.flow_rate_cali_start = MagicMock(return_value=(True, "SEQ-CALI-2"))
    c.extrusion_cali_set = MagicMock(return_value=True)
    c.extrusion_cali_sel = MagicMock(return_value=True)
    c.stop_print = MagicMock(return_value=True)
    return c


# ---------- Asset resolver ----------


def test_resolve_asset_path_pa_line_0_4():
    p = resolve_asset_path(CaliMode.PA_LINE, nozzle_diameter=0.4, pass_n=1)
    # File may not exist yet — we test the path shape
    assert p.name == "pa_line_0.4.3mf"
    assert "pressure_advance" in str(p)


def test_resolve_asset_path_flow_pass2():
    p = resolve_asset_path(CaliMode.FLOW_RATE, nozzle_diameter=0.4, pass_n=2)
    assert p.name == "flowrate_pass2_0.4.3mf"


def test_resolve_asset_path_unknown_mode():
    with pytest.raises(ValueError, match="No asset mapping"):
        resolve_asset_path(CaliMode.AUTO_PA_LINE, nozzle_diameter=0.4)


# ---------- start_calibration ----------


@pytest.mark.asyncio
async def test_start_calibration_auto_pa(service, db_session, printer_factory, mock_client):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        session = await service.start_calibration(
            db=db_session,
            printer_id=printer.id,
            cali_mode=CaliMode.AUTO_PA_LINE,
            method=CaliMethod.AUTO,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            filaments=[
                CalibFilamentInput(
                    ams_id=0,
                    slot_id=0,
                    tray_id=0,
                    filament_id="GFG00",
                    filament_setting_id="GFG00_60@BBL",
                    bed_temp=60,
                    nozzle_temp=220,
                    max_volumetric_speed=12.0,
                )
            ],
            user_id=None,
        )
    assert session.status == "running"
    assert session.method == "auto"
    assert session.cali_mode == "auto_pa_line"
    assert session.mqtt_sequence_id == "SEQ-CALI-1"
    mock_client.extrusion_cali_start.assert_called_once()
    call = mock_client.extrusion_cali_start.call_args.kwargs
    assert call["cali_mode"] == 0
    assert call["filaments"][0]["filament_id"] == "GFG00"
    assert call["filaments"][0]["nozzle_id"] == "HS20"


@pytest.mark.asyncio
async def test_start_calibration_blocks_on_offline(service, db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = None
        with pytest.raises(ValueError, match="not online"):
            await service.start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode.AUTO_PA_LINE,
                method=CaliMethod.AUTO,
                nozzle_diameter=0.4,
                nozzle_volume_type="standard",
                extruder_id=0,
                filaments=[],
                user_id=None,
            )


@pytest.mark.asyncio
async def test_start_calibration_concurrent_blocked(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    printer = await printer_factory(model="X1C")
    fil = CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0)
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        await service.start_calibration(
            db=db_session,
            printer_id=printer.id,
            cali_mode=CaliMode.AUTO_PA_LINE,
            method=CaliMethod.AUTO,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            filaments=[fil],
            user_id=None,
        )
        with pytest.raises(ValueError, match="active_session_exists"):
            await service.start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode.AUTO_PA_LINE,
                method=CaliMethod.AUTO,
                nozzle_diameter=0.4,
                nozzle_volume_type="standard",
                extruder_id=0,
                filaments=[fil],
                user_id=None,
            )


@pytest.mark.asyncio
async def test_start_calibration_auto_flow_rate(service, db_session, printer_factory, mock_client):
    """Auto Flow Rate routes to flow_rate_cali_start with flow_rate populated."""
    printer = await printer_factory(model="X1C")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        session = await service.start_calibration(
            db=db_session,
            printer_id=printer.id,
            cali_mode=CaliMode.FLOW_RATE,
            method=CaliMethod.AUTO,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            filaments=[
                CalibFilamentInput(
                    ams_id=0,
                    slot_id=0,
                    tray_id=0,
                    filament_id="GFG00",
                    filament_setting_id="GFG00_60@BBL",
                    bed_temp=60,
                    nozzle_temp=220,
                    max_volumetric_speed=12.0,
                    flow_rate=0.98,
                )
            ],
            user_id=None,
        )
    assert session.method == "auto"
    assert session.cali_mode == "flow_rate"
    mock_client.flow_rate_cali_start.assert_called_once()
    call = mock_client.flow_rate_cali_start.call_args.kwargs
    assert call["filaments"][0]["flow_rate"] == 0.98


@pytest.mark.asyncio
async def test_start_calibration_manual_enqueues_print(
    service,
    db_session,
    printer_factory,
    mock_client,
    tmp_path,
):
    printer = await printer_factory(model="P1S")
    fake_asset = tmp_path / "pa_line_0.4.3mf"
    fake_asset.write_bytes(b"PK\x03\x04fake3mf")

    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.resolve_asset_path",
            return_value=fake_asset,
        ),
        patch(
            "backend.app.services.background_dispatch.enqueue_calibration_print", new=AsyncMock(return_value=42)
        ) as enq,
    ):
        pm.get_client.return_value = mock_client
        session = await service.start_calibration(
            db=db_session,
            printer_id=printer.id,
            cali_mode=CaliMode.PA_LINE,
            method=CaliMethod.MANUAL,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            extruder_id=0,
            filaments=[CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0)],
            user_id=None,
        )
    assert session.method == "manual"
    assert session.print_queue_item_id == 42
    enq.assert_called_once()


# ---------- submit_manual_result ----------


@pytest.mark.asyncio
async def test_submit_manual_pa_computes_k(service, db_session, printer_factory, mock_client):
    """PA Line: K = 0.0 + 24 * 0.002 = 0.048."""
    printer = await printer_factory(model="P1S")
    s = CalibrationSession(
        printer_id=printer.id,
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

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        out = await service.submit_manual_result(
            db=db_session,
            session_id=s.id,
            best_line_index=24,
        )
    assert len(out.saved_rows) == 1
    assert abs(out.saved_rows[0].pa_k_value - 0.048) < 1e-9
    assert out.saved_rows[0].is_active is True
    mock_client.extrusion_cali_set.assert_called_once()


@pytest.mark.asyncio
async def test_submit_manual_flow_coarse_creates_stage2(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    printer = await printer_factory(model="P1S")
    s = CalibrationSession(
        printer_id=printer.id,
        user_id=None,
        cali_mode="flow_rate",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00","setting_id":""}]',
        status="awaiting_user_input",
        stage=1,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        out = await service.submit_manual_result(
            db=db_session,
            session_id=s.id,
            coarse_modifier=10,
            skip_fine=False,
        )
    assert out.next_session_id is not None
    assert out.saved_rows == []
    await db_session.refresh(s)
    assert s.coarse_ratio is not None
    assert abs(s.coarse_ratio - 1.10) < 1e-9


@pytest.mark.asyncio
async def test_submit_manual_flow_coarse_skip_fine_saves(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    printer = await printer_factory(model="P1S")
    s = CalibrationSession(
        printer_id=printer.id,
        user_id=None,
        cali_mode="flow_rate",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00","setting_id":""}]',
        status="awaiting_user_input",
        stage=1,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        out = await service.submit_manual_result(
            db=db_session,
            session_id=s.id,
            coarse_modifier=5,
            skip_fine=True,
        )
    assert out.next_session_id is None
    assert len(out.saved_rows) == 1
    assert abs(out.saved_rows[0].flow_ratio - 1.05) < 1e-9


@pytest.mark.asyncio
async def test_submit_manual_flow_fine_saves_combined(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    printer = await printer_factory(model="P1S")
    parent = CalibrationSession(
        printer_id=printer.id,
        user_id=None,
        cali_mode="flow_rate",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json="[]",
        status="saved",
        stage=1,
        coarse_ratio=1.05,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    stage2 = CalibrationSession(
        printer_id=printer.id,
        user_id=None,
        cali_mode="flow_rate",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00","setting_id":""}]',
        status="awaiting_user_input",
        stage=2,
        parent_session_id=parent.id,
        coarse_ratio=1.05,
    )
    db_session.add(stage2)
    await db_session.commit()
    await db_session.refresh(stage2)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        out = await service.submit_manual_result(
            db=db_session,
            session_id=stage2.id,
            fine_modifier=2,
        )
    assert len(out.saved_rows) == 1
    # 1.05 * (100+2)/100 = 1.071
    assert abs(out.saved_rows[0].flow_ratio - 1.071) < 1e-9


# ---------- submit_auto_result ----------


@pytest.mark.asyncio
async def test_submit_auto_result_saves_picked_rows(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    printer = await printer_factory(model="X1C")
    s = CalibrationSession(
        printer_id=printer.id,
        user_id=None,
        cali_mode="auto_pa_line",
        method="auto",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json='[{"ams_id":0,"slot_id":0,"tray_id":0,"filament_id":"GFG00","setting_id":"GFG00_60@BBL"}]',
        status="awaiting_user_input",
        stage=1,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    mock_client.state.extrusion_cali_results = [
        ExtrusionCaliResult(
            tray_id=0,
            ams_id=0,
            slot_id=0,
            extruder_id=0,
            nozzle_diameter=0.4,
            nozzle_volume_type="standard",
            filament_id="GFG00",
            setting_id="GFG00_60@BBL",
            k_value=0.0432,
            n_coef=1.0,
            confidence=0,
        )
    ]

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        rows = await service.submit_auto_result(
            db=db_session,
            session_id=s.id,
            edits=[{"tray_id": 0, "k_value": 0.05, "name": "PLA — PA 0.05", "save": True}],
        )
    assert len(rows) == 1
    assert abs(rows[0].pa_k_value - 0.05) < 1e-9


# ---------- cancel_session ----------


@pytest.mark.asyncio
async def test_cancel_session_running_auto_stops_print(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    printer = await printer_factory(model="X1C")
    s = CalibrationSession(
        printer_id=printer.id,
        user_id=None,
        cali_mode="auto_pa_line",
        method="auto",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json="[]",
        status="running",
        stage=1,
        mqtt_sequence_id="SEQ-X",
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        await service.cancel_session(db=db_session, session_id=s.id)
    await db_session.refresh(s)
    assert s.status == "cancelled"
    mock_client.stop_print.assert_called_once()


# ---------- resolve_active_calibration ----------


@pytest.mark.asyncio
async def test_resolve_returns_active_row(db_session, printer_factory):
    from backend.app.models.filament_calibration import FilamentCalibration

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
            name="r1",
        )
    )
    await db_session.commit()

    row = await resolve_active_calibration(
        db=db_session,
        printer_model="P1S",
        filament_id="GFG00",
        nozzle_dia=0.4,
        nozzle_vol_type="standard",
        extruder_id=0,
    )
    assert row is not None
    assert row.cali_idx == 3


@pytest.mark.asyncio
async def test_resolve_no_match_returns_none(db_session, printer_factory):
    await printer_factory(model="X1C")
    row = await resolve_active_calibration(
        db=db_session,
        printer_model="X1C",
        filament_id="UNKNOWN",
        nozzle_dia=0.4,
        nozzle_vol_type="standard",
        extruder_id=0,
    )
    assert row is None
