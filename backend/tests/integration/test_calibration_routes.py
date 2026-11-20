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
from backend.app.services.calibration_mode_registry import ModeState


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
        # Mode-gating (DISABLED/VERIFICATION → 409) has its own coverage in
        # test_calibration_mode_registry.py; here we exercise the auto
        # session-start route mechanics regardless of the live registry.
        patch("backend.app.services.calibration_service.get_mode_state", return_value=ModeState.PRODUCTION),
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
        patch("backend.app.services.calibration_service.get_mode_state", return_value=ModeState.PRODUCTION),
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
                    "nozzle_id": "HS00-0.4",
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
                    "nozzle_id": "HS00-0.4",
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
            name="row1",
        )
    )
    await db_session.commit()

    r = await async_client.get(f"/api/v1/filament-calibrations?printer_id={printer.id}")
    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_set_active_flips_others(async_client, printer_factory, db_session):
    printer = await printer_factory(model="P1S")
    r1 = FilamentCalibration(
        printer_id=printer.id,
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
        printer_id=printer.id,
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
    printer = await printer_factory(model="P1S")
    row = FilamentCalibration(
        printer_id=printer.id,
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
        patch("backend.app.services.calibration_service.get_mode_state", return_value=ModeState.PRODUCTION),
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


# --- The calibration plate (spec slicing-setup r5 §8.2) ---------------------
#
# The three calibration doors (start, slice-only, bake-only) take the six-plate
# BedType, check it against the slicer the request resolves to BEFORE the
# builder runs, and hand one effective plate — the nested ``spec.bed_type``
# when it is set, else the top-level one — to both the 3MF builder and the CLI.

_FILAMENT = {
    "ams_id": 0,
    "slot_id": 0,
    "tray_id": 0,
    "filament_id": "GFG00",
    "filament_setting_id": "GFG00_60@BBL",
    "bed_temp": 60,
    "nozzle_temp": 220,
    "max_volumetric_speed": 12.0,
}
_PRESETS = {
    "printer_preset": {"source": "standard", "id": "Bambu Lab X1 Carbon 0.4 nozzle"},
    "process_preset": {"source": "standard", "id": "0.20mm Standard @BBL X1C"},
    "filament_presets": [{"source": "standard", "id": "Bambu PETG Basic @BBL X1C"}],
}
_OLD_PLATES = ["Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Supertack Plate"]


@pytest.fixture
def calib_slice(monkeypatch):
    """Record what the builder and the CLI are handed; the slice stops there."""
    from backend.app.api.routes import filament_calibration as route
    from backend.app.services import calib_3mf_builder, calibration_service, preset_resolver, slicer_api, slicer_routing

    seen: dict = {"preferred": "bambu_studio", "builder": [], "cli": []}

    async def online(db):
        return True

    async def resolve(db, *, slicer_override=None):
        return (slicer_override or seen["preferred"], "http://sidecar.invalid")

    def build(*, cali_mode, spec, extruder_count, pass_n):
        seen["builder"].append(spec.get("bed_type"))
        return b"not-a-zip"

    async def preset(db, user, ref, slot):
        return "{}"

    async def stall(db):
        return 60

    class Svc:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def slice_with_profiles(self, **kw):
            seen["cli"].append(kw.get("bed_type"))
            raise slicer_api.SlicerApiError("stopped by the test")

    for mod in (route, slicer_routing):
        monkeypatch.setattr(mod, "any_sidecar_online", online)
        monkeypatch.setattr(mod, "resolve_sidecar_url", resolve)
    for mod in (route, calib_3mf_builder):
        monkeypatch.setattr(mod, "build_calibration_3mf", build)
    for mod in (route, preset_resolver):
        monkeypatch.setattr(mod, "resolve_preset_ref", preset)
    for mod in (route, slicer_api):
        monkeypatch.setattr(mod, "SlicerApiService", Svc)
        monkeypatch.setattr(mod, "get_stall_timeout_seconds", stall)
    monkeypatch.setattr(route, "get_mode_state", lambda mode: ModeState.VERIFICATION)
    monkeypatch.setattr(calibration_service, "get_mode_state", lambda mode: ModeState.PRODUCTION)
    client = _mock_client()
    monkeypatch.setattr(calibration_service.printer_manager, "get_client", lambda pid: client)
    return seen


async def _door(async_client, printer_id: int, door: str, **fields):
    if door == "start":
        body = {
            "cali_mode": "pa_tower",
            "method": "manual",
            "nozzle_diameter": 0.4,
            "nozzle_volume_type": "standard",
            "filaments": [_FILAMENT],
            **_PRESETS,
            **fields,
        }
        return await async_client.post(f"/api/v1/printers/{printer_id}/calibration/sessions", json=body)
    body = {"cali_mode": "pa_tower", **fields}
    if door == "slice":
        body.update(_PRESETS)
    return await async_client.post(f"/api/v1/printers/{printer_id}/calibration/{door}-only", json=body)


@pytest.mark.asyncio
async def test_textured_cool_plate_accepted_with_orca_on_start_slice_and_bake(
    async_client, printer_factory, calib_slice
):
    p = await printer_factory(model="X1C")
    for door in ("start", "slice", "bake"):
        calib_slice["builder"].clear()
        calib_slice["cli"].clear()
        r = await _door(async_client, p.id, door, slicer="orcaslicer", bed_type="Textured Cool Plate")
        assert r.status_code != 422, (door, r.text)
        assert calib_slice["builder"] == ["Textured Cool Plate"], door
        assert calib_slice["cli"] == ([] if door == "bake" else ["Textured Cool Plate"]), door


@pytest.mark.asyncio
async def test_textured_cool_plate_refused_for_bambu_explicit_and_default(async_client, printer_factory, calib_slice):
    p = await printer_factory(model="X1C")
    for door in ("start", "slice", "bake"):
        explicit = await _door(async_client, p.id, door, slicer="bambu_studio", bed_type="Textured Cool Plate")
        assert explicit.status_code == 422, (door, explicit.text)
        assert "OrcaSlicer" in explicit.text
        default = await _door(async_client, p.id, door, bed_type="Textured Cool Plate")
        assert default.status_code == 422, (door, default.text)
        nested = await _door(async_client, p.id, door, spec={"bed_type": "Textured Cool Plate"})
        assert nested.status_code == 422, (door, nested.text)


@pytest.mark.asyncio
async def test_refusal_calls_no_builder_no_slice_and_writes_no_session(
    async_client, printer_factory, calib_slice, db_session
):
    p = await printer_factory(model="X1C")
    for door in ("start", "slice", "bake"):
        r = await _door(async_client, p.id, door, slicer="bambu_studio", bed_type="Textured Cool Plate")
        assert r.status_code == 422, (door, r.text)
    assert calib_slice["builder"] == []
    assert calib_slice["cli"] == []
    sessions = (await db_session.execute(select(CalibrationSession).where(CalibrationSession.printer_id == p.id))).all()
    assert sessions == []


@pytest.mark.asyncio
async def test_spec_and_top_level_conflict_same_value_to_builder_and_cli(async_client, printer_factory, calib_slice):
    p = await printer_factory(model="X1C")
    for nested, top in (("Engineering Plate", "Cool Plate"), ("Cool Plate", "Engineering Plate")):
        for door in ("start", "slice"):
            calib_slice["builder"].clear()
            calib_slice["cli"].clear()
            await _door(async_client, p.id, door, slicer="bambu_studio", bed_type=top, spec={"bed_type": nested})
            assert calib_slice["builder"] == [nested], (door, nested)
            assert calib_slice["cli"] == [nested], (door, nested)


@pytest.mark.asyncio
async def test_invalid_nested_plate_is_refused(async_client, printer_factory, calib_slice):
    p = await printer_factory(model="X1C")
    for door in ("start", "slice", "bake"):
        r = await _door(async_client, p.id, door, slicer="orcaslicer", spec={"bed_type": "Glass Plate"})
        assert r.status_code == 422, (door, r.text)
        assert "Unknown bed plate" in r.text
    assert calib_slice["builder"] == []


@pytest.mark.asyncio
async def test_missing_or_empty_nested_plate_uses_top_level(async_client, printer_factory, calib_slice):
    p = await printer_factory(model="X1C")
    for spec in (None, {}, {"bed_type": ""}):
        calib_slice["builder"].clear()
        calib_slice["cli"].clear()
        fields = {"slicer": "bambu_studio", "bed_type": "High Temp Plate"}
        if spec is not None:
            fields["spec"] = spec
        await _door(async_client, p.id, "slice", **fields)
        assert calib_slice["builder"] == ["High Temp Plate"], spec
        assert calib_slice["cli"] == ["High Temp Plate"], spec


@pytest.mark.asyncio
async def test_five_old_plates_unchanged(async_client, printer_factory, calib_slice):
    p = await printer_factory(model="X1C")
    for plate in _OLD_PLATES:
        for slicer in ("bambu_studio", "orcaslicer"):
            calib_slice["builder"].clear()
            calib_slice["cli"].clear()
            r = await _door(async_client, p.id, "slice", slicer=slicer, bed_type=plate)
            assert r.status_code != 422, (plate, slicer, r.text)
            assert calib_slice["builder"] == [plate] and calib_slice["cli"] == [plate], (plate, slicer)
