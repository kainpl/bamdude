"""Tests CalibrationService — start/submit/save/cancel orchestration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.calibration_session import CalibrationSession
from backend.app.services.bambu_mqtt import ExtrusionCaliResult
from backend.app.services.calibration_constants import CaliMethod, CaliMode
from backend.app.services.calibration_service import (
    CalibFilamentInput,
    CalibrationService,
    apply_active_calibration_to_slot,
    derive_effective_filament_id,
    parse_nozzle_vol_type,
    resolve_active_calibration,
    resolve_asset,
    sync_printer_kprofiles_to_cache,
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


def test_resolve_asset_pa_pattern_is_ready_3mf():
    """PA Pattern ships as a pre-sliced 3MF — no slicer needed."""
    asset = resolve_asset(CaliMode.PA_PATTERN)
    assert asset.path.name == "pa_pattern.3mf"
    assert asset.kind == "3mf"
    assert asset.requires_slicing is False
    assert asset.path.exists(), "BS-mirrored asset should be present in repo"


def test_resolve_asset_flow_pass1_and_pass2():
    """Flow Rate uses two pre-sliced 3MFs (coarse + fine refinement)."""
    p1 = resolve_asset(CaliMode.FLOW_RATE, pass_n=1)
    p2 = resolve_asset(CaliMode.FLOW_RATE, pass_n=2)
    assert p1.path.name == "flowrate-test-pass1.3mf"
    assert p2.path.name == "flowrate-test-pass2.3mf"
    assert p1.requires_slicing is False
    assert p2.requires_slicing is False


def test_resolve_asset_auto_pa_single_vs_dual():
    single = resolve_asset(CaliMode.AUTO_PA_LINE, extruder_count=1)
    dual = resolve_asset(CaliMode.AUTO_PA_LINE, extruder_count=2)
    assert single.path.name == "auto_pa_line_single.3mf"
    assert dual.path.name == "auto_pa_line_dual.3mf"
    assert single.requires_slicing is False
    assert dual.requires_slicing is False


def test_resolve_asset_pa_line_requires_slicing():
    """PA Line (and PA Tower) ship as STLs from BS — slicer pipeline needed."""
    asset = resolve_asset(CaliMode.PA_LINE)
    assert asset.kind == "stl"
    assert asset.requires_slicing is True
    assert asset.path.exists()


def test_resolve_asset_tower_modes_require_slicing():
    for mode in (
        CaliMode.PA_TOWER,
        CaliMode.TEMP_TOWER,
        CaliMode.VFA_TOWER,
        CaliMode.RETRACTION_TOWER,
    ):
        asset = resolve_asset(mode)
        assert asset.kind == "stl", f"{mode}: expected STL"
        assert asset.requires_slicing is True, f"{mode}: should require slicing"
        assert asset.path.exists(), f"{mode}: BS asset should be mirrored"


def test_resolve_asset_vol_speed_is_step():
    """VolSpeed ships as a STEP file from BS — needs slicing (and step→mesh)."""
    asset = resolve_asset(CaliMode.VOL_SPEED_TOWER)
    assert asset.path.name == "SpeedTestStructure.step"
    assert asset.kind == "step"
    assert asset.requires_slicing is True


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
    from backend.app.services.calibration_service import CalibAsset

    printer = await printer_factory(model="P1S")
    fake_asset_path = tmp_path / "pa_pattern.3mf"
    fake_asset_path.write_bytes(b"PK\x03\x04fake3mf")
    fake_asset = CalibAsset(path=fake_asset_path, kind="3mf", requires_slicing=False)

    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.resolve_asset",
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
            cali_mode=CaliMode.PA_PATTERN,
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


@pytest.mark.asyncio
async def test_start_calibration_manual_rejects_stl_mode_without_sidecar(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    """PA Line / Tower modes require slicing — must raise until W2 lands."""
    from backend.app.services.calibration_service import SlicerSidecarRequiredError

    printer = await printer_factory(model="P1S")
    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        with pytest.raises(SlicerSidecarRequiredError):
            await service.start_calibration(
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

    printer = await printer_factory(model="P1S")
    db_session.add(
        FilamentCalibration(
            printer_id=printer.id,
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
        printer_id=printer.id,
        filament_id="GFG00",
        nozzle_dia=0.4,
        nozzle_vol_type="standard",
        extruder_id=0,
    )
    assert row is not None
    assert row.cali_idx == 3


@pytest.mark.asyncio
async def test_resolve_no_match_returns_none(db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    row = await resolve_active_calibration(
        db=db_session,
        printer_id=printer.id,
        filament_id="UNKNOWN",
        nozzle_dia=0.4,
        nozzle_vol_type="standard",
        extruder_id=0,
    )
    assert row is None


# ---------- parse_nozzle_vol_type ----------


def test_parse_nozzle_vol_type_known_prefixes():
    assert parse_nozzle_vol_type("HS00-0.4") == "standard"
    assert parse_nozzle_vol_type("HH00-0.4") == "high_flow"
    assert parse_nozzle_vol_type("HU00-0.2") == "tpu_high_flow"
    assert parse_nozzle_vol_type("HY00-0.8") == "hybrid"


def test_parse_nozzle_vol_type_fallbacks():
    assert parse_nozzle_vol_type(None) == "standard"
    assert parse_nozzle_vol_type("") == "standard"
    assert parse_nozzle_vol_type("XX99-0.4") == "standard"


# ---------- derive_effective_filament_id ----------


def test_derive_effective_filament_id_prefers_bambu_id():
    spool = MagicMock(bambu_filament_id="GFG96", slicer_filament="GFSL05")
    assert derive_effective_filament_id(spool=spool, slot_tray_info_idx="GFB99") == "GFG96"


def test_derive_effective_filament_id_falls_back_to_slicer():
    spool = MagicMock(bambu_filament_id=None, slicer_filament="GFSL05_07")
    # normalize_slicer_filament strips version suffix and inverts S
    assert derive_effective_filament_id(spool=spool, slot_tray_info_idx="GFB99") == "GFL05"


def test_derive_effective_filament_id_falls_back_to_slot():
    spool = MagicMock(bambu_filament_id=None, slicer_filament=None)
    assert derive_effective_filament_id(spool=spool, slot_tray_info_idx="GFB99") == "GFB99"


def test_derive_effective_filament_id_returns_none_when_nothing():
    assert derive_effective_filament_id(spool=None, slot_tray_info_idx=None) is None


# ---------- apply_active_calibration_to_slot ----------


@pytest.mark.asyncio
async def test_apply_resolves_combo_and_matches_live_kprofile(db_session, printer_factory):
    """Live cali_idx differs from cached cali_idx — bind must use the LIVE one
    after stable-identity match against client.state.kprofiles."""
    from backend.app.models.filament_calibration import FilamentCalibration

    printer = await printer_factory(model="X1C")
    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id="GFG96",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.025,
        cali_mode="pa_line",
        source="manual",
        is_active=True,
        cali_idx=3,
        name="PETG-HF K=0.025",
    )
    db_session.add(fc)
    await db_session.commit()

    live_kp = MagicMock(
        slot_id=5,
        k_value="0.025000",
        filament_id="GFG96",
        extruder_id=0,
        nozzle_diameter="0.4",
    )
    live_kp.name = "PETG-HF K=0.025"  # `name` is a MagicMock-reserved kwarg
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = [live_kp]
    client.extrusion_cali_sel = MagicMock(return_value=(True, "0"))

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=0,
            filament_id="GFG96",
            nozzle_diameter=0.4,
        )

    assert fired is True
    assert row is not None and row.id == fc.id
    args = client.extrusion_cali_sel.call_args.kwargs
    assert args["cali_idx"] == 5  # live slot, NOT the stale cached cali_idx=3


@pytest.mark.asyncio
async def test_apply_returns_false_when_no_active_row(db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = []

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=0,
            filament_id="GFG96",
            nozzle_diameter=0.4,
        )
    assert fired is False
    assert row is None


@pytest.mark.asyncio
async def test_apply_returns_false_when_cache_stale(db_session, printer_factory):
    """Cache has a row, but printer's live list has nothing matching → no bind,
    but the cache row is still returned so the caller knows."""
    from backend.app.models.filament_calibration import FilamentCalibration

    printer = await printer_factory(model="X1C")
    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id="GFG96",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.025,
        cali_mode="pa_line",
        source="manual",
        is_active=True,
        name="PETG-HF K=0.025",
    )
    db_session.add(fc)
    await db_session.commit()

    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = []

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=0,
            filament_id="GFG96",
            nozzle_diameter=0.4,
        )
    assert fired is False
    assert row is not None
    assert row.id == fc.id


@pytest.mark.asyncio
async def test_apply_returns_false_when_client_disconnected(db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    client = MagicMock()
    client.state.connected = False

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        fired, row = await apply_active_calibration_to_slot(
            db=db_session,
            printer_id=printer.id,
            ams_id=0,
            slot_id=0,
            filament_id="GFG96",
            nozzle_diameter=0.4,
        )
    assert fired is False
    assert row is None


# ---------- sync_printer_kprofiles_to_cache ----------


@pytest.mark.asyncio
async def test_sync_creates_new_cache_rows_inactive(db_session, printer_factory):
    """Printer reports a profile we don't have cached → create row, is_active=False."""
    from sqlalchemy import select

    from backend.app.models.filament_calibration import FilamentCalibration

    printer = await printer_factory(model="X1C")
    live_kp = MagicMock(
        slot_id=2,
        k_value="0.025",
        filament_id="GFG96",
        extruder_id=0,
        nozzle_diameter="0.4",
        nozzle_id="HS00-0.4",
        setting_id="GFSG96",
    )
    live_kp.name = "PETG-HF K=0.025"
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = [live_kp]

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        touched = await sync_printer_kprofiles_to_cache(db=db_session, printer_id=printer.id)

    assert touched == 1
    rows = (
        (await db_session.execute(select(FilamentCalibration).where(FilamentCalibration.printer_id == printer.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].is_active is False
    assert rows[0].source == "printer_sync"
    assert rows[0].cali_idx == 2
    assert rows[0].pa_k_value == 0.025


@pytest.mark.asyncio
async def test_sync_refreshes_stale_cali_idx_on_existing_row(db_session, printer_factory):
    """Existing cache row has stale cali_idx → sync updates it."""
    from backend.app.models.filament_calibration import FilamentCalibration

    printer = await printer_factory(model="X1C")
    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id="GFG96",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.025,
        cali_mode="pa_line",
        source="manual",
        is_active=True,
        cali_idx=2,
        name="PETG-HF K=0.025",
    )
    db_session.add(fc)
    await db_session.commit()

    # Printer reordered — same identity, different slot
    live_kp = MagicMock(
        slot_id=7,
        k_value="0.025",
        filament_id="GFG96",
        extruder_id=0,
        nozzle_diameter="0.4",
        nozzle_id="HS00-0.4",
        setting_id="GFSG96",
    )
    live_kp.name = "PETG-HF K=0.025"
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = [live_kp]

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        touched = await sync_printer_kprofiles_to_cache(db=db_session, printer_id=printer.id)

    assert touched == 1
    await db_session.refresh(fc)
    assert fc.cali_idx == 7
    assert fc.is_active is True  # sync doesn't touch is_active on existing rows


@pytest.mark.asyncio
async def test_sync_idempotent_when_already_in_sync(db_session, printer_factory):
    """Cache + live agree → no touches."""
    from backend.app.models.filament_calibration import FilamentCalibration

    printer = await printer_factory(model="X1C")
    fc = FilamentCalibration(
        printer_id=printer.id,
        filament_id="GFG96",
        filament_setting_id="GFSG96",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.025,
        cali_mode="pa_line",
        source="manual",
        is_active=True,
        cali_idx=5,
        name="PETG-HF K=0.025",
        nozzle_id="HS00-0.4",
    )
    db_session.add(fc)
    await db_session.commit()

    live_kp = MagicMock(
        slot_id=5,
        k_value="0.025",
        filament_id="GFG96",
        extruder_id=0,
        nozzle_diameter="0.4",
        nozzle_id="HS00-0.4",
        setting_id="GFSG96",
    )
    live_kp.name = "PETG-HF K=0.025"
    client = MagicMock()
    client.state.connected = True
    client.state.kprofiles = [live_kp]

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = client
        touched = await sync_printer_kprofiles_to_cache(db=db_session, printer_id=printer.id)
    assert touched == 0
