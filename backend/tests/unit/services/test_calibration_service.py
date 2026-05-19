"""Tests CalibrationService — start/submit/save/cancel orchestration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.filament_calibration import FilamentCalibration
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


def test_resolve_asset_pa_pattern():
    """PA Pattern uses BS pa_pattern.3mf as scaffold (then sliced under
    user's filament profile by the W2 slicer pipeline)."""
    asset = resolve_asset(CaliMode.PA_PATTERN)
    assert asset.path.name == "pa_pattern.3mf"
    assert asset.kind == "3mf"
    assert asset.path.exists()


def test_resolve_asset_flow_pass1_and_pass2():
    p1 = resolve_asset(CaliMode.FLOW_RATE, pass_n=1)
    p2 = resolve_asset(CaliMode.FLOW_RATE, pass_n=2)
    assert p1.path.name == "flowrate-test-pass1.3mf"
    assert p2.path.name == "flowrate-test-pass2.3mf"


def test_resolve_asset_auto_pa_single_vs_dual():
    single = resolve_asset(CaliMode.AUTO_PA_LINE, extruder_count=1)
    dual = resolve_asset(CaliMode.AUTO_PA_LINE, extruder_count=2)
    assert single.path.name == "auto_pa_line_single.3mf"
    assert dual.path.name == "auto_pa_line_dual.3mf"


def test_resolve_asset_stl_modes_exist():
    """STL/STEP scaffold geometry is mirrored verbatim from BS resources/calib/.

    PA_LINE switched to the shared pa_pattern.3mf cube scaffold when the
    Python port landed (custom_gcode injection model + corner-parked
    cube placeholder), so it's tested under the 3MF group below instead.
    """
    for mode in (
        CaliMode.PA_TOWER,
        CaliMode.TEMP_TOWER,
        CaliMode.VFA_TOWER,
        CaliMode.RETRACTION_TOWER,
        CaliMode.VOL_SPEED_TOWER,
    ):
        asset = resolve_asset(mode)
        assert asset.kind in ("stl", "step"), f"{mode}: expected STL/STEP"
        assert asset.path.exists(), f"{mode}: BS asset should be mirrored"


def test_resolve_asset_pa_line_shares_pattern_scaffold():
    """PA_LINE rides the PA Pattern cube scaffold (3MF, not the BS
    pressure_advance_test.stl). The cube is the placeholder our
    one-layer custom_gcode injects against."""
    asset = resolve_asset(CaliMode.PA_LINE)
    assert asset.kind == "3mf"
    assert asset.path.name == "pa_pattern.3mf"
    assert asset.path.exists()


# ---------- start_calibration ----------


@pytest.mark.asyncio
async def test_start_calibration_auto_pa(service, db_session, printer_factory, mock_client):
    from backend.app.services.calibration_mode_registry import ModeState

    printer = await printer_factory(model="X1C")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.PRODUCTION,
        ),
    ):
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
    from backend.app.services.calibration_mode_registry import ModeState

    printer = await printer_factory(model="X1C")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.PRODUCTION,
        ),
    ):
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
    from backend.app.services.calibration_mode_registry import ModeState

    printer = await printer_factory(model="X1C")
    fil = CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0)
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.PRODUCTION,
        ),
    ):
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
    from backend.app.services.calibration_mode_registry import ModeState

    printer = await printer_factory(model="X1C")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.PRODUCTION,
        ),
    ):
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
    """W2 manual path: bake the per-mode 3MF → slice through sidecar →
    persist sliced bytes as a LibraryFile → enqueue via
    ``background_dispatch.enqueue_calibration_print`` with the right
    library_file_id + back-reference to the calibration session.

    All external boundaries mocked: the sidecar (``SlicerApiService``),
    preset resolution (``resolve_preset_ref``), the slicer routing
    health check, and the dispatcher's enqueue. Anything inside our
    service boundary (mode registry, build_calibration_3mf, session row
    creation, atomic order of session + queue item creation) runs for
    real so the test fails loudly when that wiring drifts."""
    from backend.app.schemas.slicer import PresetRef
    from backend.app.services.calibration_mode_registry import ModeState
    from backend.app.services.calibration_service import CalibAsset
    from backend.app.services.slicer_api import SliceResult

    printer = await printer_factory(model="P1S")
    fake_asset_path = tmp_path / "pa_pattern.3mf"
    fake_asset_path.write_bytes(b"PK\x03\x04fake3mf")
    fake_asset = CalibAsset(path=fake_asset_path, kind="3mf")

    # Stand in for SlicerApiService's async context manager: ``async with
    # SlicerApiService(base_url=...) as svc`` → svc.slice_with_profiles
    # returns SliceResult.
    fake_slice_result = SliceResult(
        content=b"sliced gcode 3mf bytes",
        print_time_seconds=600,
        filament_used_g=5.0,
        filament_used_mm=1500.0,
    )
    svc_mock = MagicMock()
    svc_mock.slice_with_profiles = AsyncMock(return_value=fake_slice_result)
    svc_ctx_mock = MagicMock()
    svc_ctx_mock.__aenter__ = AsyncMock(return_value=svc_mock)
    svc_ctx_mock.__aexit__ = AsyncMock(return_value=None)

    preset_ref = PresetRef(source="standard", id="dummy")

    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.resolve_asset",
            return_value=fake_asset,
        ),
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.PRODUCTION,
        ),
        # build_calibration_3mf is imported inside start_calibration —
        # patch at the source module, not the importing one.
        patch(
            "backend.app.services.calib_3mf_builder.build_calibration_3mf",
            return_value=b"baked 3mf bytes",
        ),
        patch(
            "backend.app.services.slicer_routing.any_sidecar_online",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "backend.app.services.slicer_routing.resolve_sidecar_url",
            new=AsyncMock(return_value=("orcaslicer", "http://localhost:3001")),
        ),
        patch(
            "backend.app.services.preset_resolver.resolve_preset_ref",
            new=AsyncMock(return_value='{"name":"dummy","type":"printer"}'),
        ),
        patch("backend.app.services.slicer_api.SlicerApiService", return_value=svc_ctx_mock),
        patch(
            "backend.app.services.calibration_service._persist_calibration_slice_to_library",
            new=AsyncMock(return_value=99),
        ) as persist,
        patch(
            "backend.app.services.background_dispatch.enqueue_calibration_print",
            new=AsyncMock(return_value=42),
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
            printer_preset=preset_ref,
            process_preset=preset_ref,
            filament_presets=[preset_ref],
        )
    assert session.method == "manual"
    assert session.print_queue_item_id == 42
    enq.assert_called_once()
    # Library file came from the persisted sliced bytes — the dispatcher
    # received that id, not the bake bytes directly.
    persist.assert_awaited_once()
    enqueue_kwargs = enq.call_args.kwargs
    assert enqueue_kwargs["library_file_id"] == 99
    assert enqueue_kwargs["calibration_session_id"] == session.id


@pytest.mark.asyncio
async def test_start_calibration_manual_rejects_without_sidecar(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    """All manual modes need slicing → reject when sidecar is offline."""
    from backend.app.services.calibration_mode_registry import ModeState
    from backend.app.services.calibration_service import SlicerSidecarRequiredError

    printer = await printer_factory(model="P1S")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.PRODUCTION,
        ),
        patch("backend.app.services.slicer_routing.any_sidecar_online", new=AsyncMock(return_value=False)),
    ):
        pm.get_client.return_value = mock_client
        with pytest.raises(SlicerSidecarRequiredError):
            await service.start_calibration(
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


@pytest.mark.asyncio
async def test_start_calibration_rejects_disabled_mode(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    """MODE_STATE DISABLED → CalibModeNotImplementedError before any other check.

    The mode state is mocked rather than picking a currently-disabled
    mode, so the test is stable as the W2 rollout flips modes on.
    """
    from backend.app.services.calibration_mode_registry import ModeState
    from backend.app.services.calibration_service import CalibModeNotImplementedError

    printer = await printer_factory(model="P1S")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch("backend.app.services.calibration_service.get_mode_state", return_value=ModeState.DISABLED),
    ):
        pm.get_client.return_value = mock_client
        with pytest.raises(CalibModeNotImplementedError):
            await service.start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode.PA_TOWER,
                method=CaliMethod.MANUAL,
                nozzle_diameter=0.4,
                nozzle_volume_type="standard",
                extruder_id=0,
                filaments=[CalibFilamentInput(0, 0, 0, "GFG00", "GFG00_60@BBL", 60, 220, 12.0)],
                user_id=None,
            )


@pytest.mark.asyncio
async def test_start_calibration_rejects_verification_mode(
    service,
    db_session,
    printer_factory,
    mock_client,
):
    """MODE_STATE VERIFICATION → CalibModeVerificationOnlyError; wizard must
    route the request through ``POST .../calibration/slice-only`` instead."""
    from backend.app.services.calibration_mode_registry import ModeState
    from backend.app.services.calibration_service import CalibModeVerificationOnlyError

    printer = await printer_factory(model="P1S")
    with (
        patch("backend.app.services.calibration_service.printer_manager") as pm,
        patch(
            "backend.app.services.calibration_service.get_mode_state",
            return_value=ModeState.VERIFICATION,
        ),
    ):
        pm.get_client.return_value = mock_client
        with pytest.raises(CalibModeVerificationOnlyError):
            await service.start_calibration(
                db=db_session,
                printer_id=printer.id,
                cali_mode=CaliMode.PA_TOWER,
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
            fine_modifier=-2,
        )
    assert len(out.saved_rows) == 1
    # 1.05 * (100-2)/100 = 1.029
    assert abs(out.saved_rows[0].flow_ratio - 1.029) < 1e-9


# ---------- submit_manual_result — tower modes ----------


def _tower_session(printer_id: int, *, cali_mode: str = "vfa_tower", status: str = "saved") -> CalibrationSession:
    """A tower-mode calibration session ready for a manual result.

    Tower prints land the session at ``saved`` (the on-complete handler
    flips it straight there) — the operator records the measured result
    from the finish page afterwards.
    """
    return CalibrationSession(
        printer_id=printer_id,
        user_id=None,
        cali_mode=cali_mode,
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        filaments_json=json.dumps(
            [{"ams_id": 0, "slot_id": 0, "tray_id": 0, "filament_id": "GFG00", "setting_id": ""}]
        ),
        status=status,
        stage=1,
    )


@pytest.mark.asyncio
async def test_submit_manual_tower_saves_inert_row(service, db_session, printer_factory, mock_client):
    """A VFA tower result lands as an is_active=False filament_calibration
    row — tower_result set, pa_k_value / flow_ratio NULL, no MQTT push."""
    printer = await printer_factory(model="P1S")
    s = _tower_session(printer.id)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        out = await service.submit_manual_result(db=db_session, session_id=s.id, tower_result=130.0)

    assert len(out.saved_rows) == 1
    row = out.saved_rows[0]
    assert row.tower_result == 130.0
    assert row.pa_k_value is None
    assert row.flow_ratio is None
    assert row.is_active is False
    assert row.cali_mode == "vfa_tower"
    assert row.source == "manual"
    # A tower row is an inert farm record — nothing is pushed to the printer.
    mock_client.extrusion_cali_set.assert_not_called()
    mock_client.extrusion_cali_sel.assert_not_called()


@pytest.mark.asyncio
async def test_submit_manual_tower_requires_tower_result(service, db_session, printer_factory):
    """Tower modes need a tower_result — without one, submit raises."""
    printer = await printer_factory(model="P1S")
    s = _tower_session(printer.id, cali_mode="vol_speed_tower")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)

    with pytest.raises(ValueError, match="tower_result required"):
        await service.submit_manual_result(db=db_session, session_id=s.id)


@pytest.mark.asyncio
async def test_submit_manual_tower_keeps_pa_row_active(service, db_session, printer_factory, mock_client):
    """Saving a tower result must NOT deactivate the combo's active PA row.

    The tower row is inert (is_active=False) — it never touches the
    partial-unique active index, so the K-profile row stays in force.
    """
    printer = await printer_factory(model="P1S")
    pa_row = FilamentCalibration(
        printer_id=printer.id,
        filament_id="GFG00",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.025,
        cali_mode="pa_tower",
        source="manual",
        is_active=True,
        name="pa_tower K=0.0250",
    )
    db_session.add(pa_row)
    s = _tower_session(printer.id)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(pa_row)
    await db_session.refresh(s)

    with patch("backend.app.services.calibration_service.printer_manager") as pm:
        pm.get_client.return_value = mock_client
        await service.submit_manual_result(db=db_session, session_id=s.id, tower_result=14.5)

    await db_session.refresh(pa_row)
    assert pa_row.is_active is True


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
