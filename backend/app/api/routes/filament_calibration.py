"""REST routes for the Filament Calibration wizard (m062 / Plan 1).

Mounted at /printers and /calibration prefixes via the single router below;
permission PRINTERS_UPDATE gates POST/DELETE, PRINTERS_READ gates GETs.
Every mutation also writes a calibration_audit row mirroring m060 + m061
audit-trail patterns.
"""

from __future__ import annotations

import json as _json
import logging
import urllib.parse

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.calibration_audit import CalibrationAudit
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.filament_calibration import (
    AutoResultIn,
    CalibBakeOnlyIn,
    CalibCapabilities,
    CalibrationSessionOut,
    CalibSliceOnlyIn,
    FilamentCalibrationOut,
    ManualResultIn,
    ManualResultOutSchema,
    PACalibHistoryEntryOut,
    StartSessionIn,
)
from backend.app.services.calib_3mf_builder import build_calibration_3mf
from backend.app.services.calibration_constants import CaliMode
from backend.app.services.calibration_mode_registry import ModeState, get_mode_state
from backend.app.services.calibration_service import (
    CalibFilamentInput,
    CalibModeNotImplementedError,
    CalibModeVerificationOnlyError,
    CalibrationService,
    SlicerSidecarRequiredError,
    reconcile_session_status,
)
from backend.app.services.preset_resolver import resolve_preset_ref
from backend.app.services.printer_capabilities import compute_calibration_supports
from backend.app.services.printer_manager import printer_manager
from backend.app.services.slicer_api import (
    SlicerApiError,
    SlicerApiService,
    SlicerApiUnavailableError,
    SlicerInputError,
)
from backend.app.services.slicer_routing import any_sidecar_online, resolve_sidecar_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["filament-calibration"])
_service = CalibrationService()


def _log_preset_compat(slot: str, ref, json_str: str) -> None:
    """Dump compatibility-relevant fields from a resolved preset JSON,
    plus the total key count and a sample of distinguishing keys so we
    can tell whether the resolver returned a thin stub or a fully-
    flattened preset.

    BS / Orca CLI prioritises embedded ``project_settings.config`` keys
    over ``--load-settings`` keys when both are present, so a thin stub
    from the resolver effectively lets the embedded N1 / PLA defaults
    win for everything the stub doesn't override. The total-key-count
    log tells us at a glance if the cloud resolver is doing its job
    (~300 keys = full preset, <20 = stub).
    """
    try:
        data = _json.loads(json_str) if isinstance(json_str, str) else json_str
    except (ValueError, TypeError):
        logger.warning("slice_only/compat: %s preset (%r) is not valid JSON", slot, ref)
        return
    if not isinstance(data, dict):
        logger.warning("slice_only/compat: %s preset (%r) JSON is not an object", slot, ref)
        return
    keys_of_interest = (
        "name",
        "type",
        "from",
        "inherits",
        "printer_settings_id",
        "printer_model",
        "printer_variant",
        "compatible_printers",
        "compatible_printers_condition",
        "compatible_prints",
        "compatible_prints_condition",
        "print_compatible_printers",
        "filament_settings_id",
        "print_settings_id",
        "filament_type",
        "nozzle_diameter",
    )
    snapshot = {k: data[k] for k in keys_of_interest if k in data}
    logger.info(
        "slice_only/compat: %s preset (ref=%r) total_keys=%d → %s",
        slot,
        ref,
        len(data),
        snapshot,
    )


async def _audit(
    db: AsyncSession,
    *,
    printer_id: int,
    user: User | None,
    action: str,
    payload: dict,
    sequence_id: str | None = None,
    session_id: int | None = None,
    filament_calibration_id: int | None = None,
    result: str = "ok",
    error: str | None = None,
) -> None:
    db.add(
        CalibrationAudit(
            printer_id=printer_id,
            user_id=user.id if user else None,
            session_id=session_id,
            filament_calibration_id=filament_calibration_id,
            action=action,
            payload_json=_json.dumps(payload, default=str),
            sequence_id=sequence_id,
            result=result,
            error_message=error,
        )
    )
    await db.commit()


# ---------- Capabilities + sessions ----------


@router.get(
    "/printers/{printer_id}/calibration/capabilities",
    response_model=CalibCapabilities,
)
async def get_capabilities(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> CalibCapabilities:
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    return CalibCapabilities(
        **compute_calibration_supports(client.state, printer.model, getattr(client, "module_vers", {}))
    )


@router.post(
    "/printers/{printer_id}/calibration/sessions",
    response_model=CalibrationSessionOut,
)
async def start_session(
    printer_id: int,
    body: StartSessionIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> CalibrationSessionOut:
    try:
        session = await _service.start_calibration(
            db=db,
            user=user,
            printer_id=printer_id,
            cali_mode=body.cali_mode,
            method=body.method,
            nozzle_diameter=body.nozzle_diameter,
            nozzle_volume_type=body.nozzle_volume_type,
            extruder_id=body.extruder_id,
            filaments=[
                CalibFilamentInput(
                    ams_id=f.ams_id,
                    slot_id=f.slot_id,
                    tray_id=f.tray_id,
                    filament_id=f.filament_id,
                    filament_setting_id=f.filament_setting_id,
                    bed_temp=f.bed_temp,
                    nozzle_temp=f.nozzle_temp,
                    max_volumetric_speed=f.max_volumetric_speed,
                    flow_rate=f.flow_rate,
                    extruder_id_override=f.extruder_id,
                )
                for f in body.filaments
            ],
            user_id=user.id if user else None,
            spec=body.spec,
            bundle=body.bundle,
            printer_preset=body.printer_preset,
            process_preset=body.process_preset,
            filament_presets=body.filament_presets,
            slicer=body.slicer,
            bed_type=body.bed_type,
            print_options=body.print_options.model_dump(),
            swap_macros=body.swap_macros.model_dump(),
        )
    except SlicerSidecarRequiredError as e:
        # Audit the rejected start so the trail still shows what the user tried.
        try:
            await _audit(
                db,
                printer_id=printer_id,
                user=user,
                action="start_session",
                payload=body.model_dump(),
                result="error",
                error=str(e),
            )
        except Exception:
            pass
        raise HTTPException(
            409,
            detail={"detail": "slicer_sidecar_required", "message": str(e)},
        )
    except CalibModeNotImplementedError as e:
        try:
            await _audit(
                db,
                printer_id=printer_id,
                user=user,
                action="start_session",
                payload=body.model_dump(),
                result="error",
                error=str(e),
            )
        except Exception:
            pass
        raise HTTPException(
            409,
            detail={"detail": "mode_not_implemented", "message": str(e)},
        )
    except CalibModeVerificationOnlyError as e:
        try:
            await _audit(
                db,
                printer_id=printer_id,
                user=user,
                action="start_session",
                payload=body.model_dump(),
                result="error",
                error=str(e),
            )
        except Exception:
            pass
        raise HTTPException(
            409,
            detail={"detail": "mode_verification_only", "message": str(e)},
        )
    except ValueError as e:
        msg = str(e)
        if msg.startswith("active_session_exists"):
            # Encode existing session id back so UI can resume / discard
            _, _, sid = msg.partition(":")
            raise HTTPException(
                409,
                detail={"detail": "active_session_exists", "session_id": int(sid) if sid else None},
            )
        # Audit failed start attempt — best-effort, swallow on race
        try:
            await _audit(
                db,
                printer_id=printer_id,
                user=user,
                action="start_session",
                payload=body.model_dump(),
                result="error",
                error=msg,
            )
        except Exception:
            pass
        raise HTTPException(400, msg)

    await _audit(
        db,
        printer_id=printer_id,
        user=user,
        action="start_session",
        payload=body.model_dump(),
        sequence_id=session.mqtt_sequence_id,
        session_id=session.id,
    )
    return CalibrationSessionOut.model_validate(session)


@router.get(
    "/calibration/sessions/{session_id}",
    response_model=CalibrationSessionOut,
)
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> CalibrationSessionOut:
    s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Session not found")
    # Lazy reconciliation: while the wizard polls this endpoint, flip
    # running → awaiting_user_input | saved | failed if printer state /
    # linked print queue item has moved past us.
    await reconcile_session_status(db, s)
    return CalibrationSessionOut.model_validate(s)


@router.get(
    "/calibration/sessions",
    response_model=list[CalibrationSessionOut],
)
async def list_sessions(
    printer_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[CalibrationSessionOut]:
    q = select(CalibrationSession)
    if printer_id is not None:
        q = q.where(CalibrationSession.printer_id == printer_id)
    if status:
        q = q.where(CalibrationSession.status == status)
    q = q.order_by(CalibrationSession.created_at.desc()).limit(50)
    rows = (await db.execute(q)).scalars().all()
    return [CalibrationSessionOut.model_validate(r) for r in rows]


@router.post("/calibration/sessions/{session_id}/cancel", status_code=204)
async def cancel_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> None:
    s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Session not found")
    try:
        await _service.cancel_session(db=db, session_id=session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit(
        db,
        printer_id=s.printer_id,
        user=user,
        action="cancel",
        payload={"session_id": session_id},
        session_id=session_id,
    )


# ---------- Submit ----------


@router.post(
    "/calibration/sessions/{session_id}/manual-result",
    response_model=ManualResultOutSchema,
)
async def submit_manual_result(
    session_id: int,
    body: ManualResultIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> ManualResultOutSchema:
    s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Session not found")
    try:
        out = await _service.submit_manual_result(
            db=db,
            session_id=session_id,
            best_line_index=body.best_line_index,
            pa_k_value=body.pa_k_value,
            coarse_modifier=body.coarse_modifier,
            skip_fine=body.skip_fine,
            fine_modifier=body.fine_modifier,
            tower_result=body.tower_result,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    await _audit(
        db,
        printer_id=s.printer_id,
        user=user,
        action="save_result",
        payload=body.model_dump(),
        session_id=session_id,
        filament_calibration_id=out.saved_rows[0].id if out.saved_rows else None,
    )
    return ManualResultOutSchema(
        saved_rows=[FilamentCalibrationOut.model_validate(r) for r in out.saved_rows],
        next_session_id=out.next_session_id,
    )


@router.post(
    "/calibration/sessions/{session_id}/auto-result",
    response_model=list[FilamentCalibrationOut],
)
async def submit_auto_result(
    session_id: int,
    body: AutoResultIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> list[FilamentCalibrationOut]:
    s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Session not found")
    try:
        rows = await _service.submit_auto_result(
            db=db,
            session_id=session_id,
            edits=[e.model_dump() for e in body.results],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    await _audit(
        db,
        printer_id=s.printer_id,
        user=user,
        action="save_result",
        payload=body.model_dump(),
        session_id=session_id,
        filament_calibration_id=rows[0].id if rows else None,
    )
    return [FilamentCalibrationOut.model_validate(r) for r in rows]


# ---------- Slice-only (verification mode) ----------


@router.post(
    "/printers/{printer_id}/calibration/slice-only",
    responses={
        200: {
            "content": {"model/3mf": {}},
            "description": "Sliced calibration .gcode.3mf as an HTTP attachment.",
        },
        409: {"description": "Mode is not in VERIFICATION state, or sidecar offline."},
    },
)
async def slice_calibration_for_verification(
    printer_id: int,
    body: CalibSliceOnlyIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> Response:
    """Bake + slice a calibration plate and return the sliced 3MF as a download.

    Reachable only when the mode's ``MODE_STATE`` entry is
    ``VERIFICATION`` — the entire purpose of this endpoint is to let the
    operator validate per-mode output against BS / Orca reference 3MFs
    before that mode flips to ``PRODUCTION`` and starts dispatching to
    real printers.

    Per-mode lifecycle:

    - ``DISABLED`` → 409 ``mode_not_implemented``.
    - ``VERIFICATION`` → run the slice, return bytes as attachment.
    - ``PRODUCTION`` → 409 ``mode_verification_not_applicable`` with a
      hint to use the standard wizard ``POST .../sessions`` flow.

    See ``temp/w2-calibration-implementation-plan.md`` §0 for the
    lifecycle contract.
    """
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    state = get_mode_state(body.cali_mode)
    if state == ModeState.DISABLED:
        await _audit(
            db,
            printer_id=printer_id,
            user=user,
            action="slice_only",
            payload=body.model_dump(),
            result="error",
            error=f"mode_not_implemented:{body.cali_mode.value}",
        )
        raise HTTPException(
            409,
            detail={
                "detail": "mode_not_implemented",
                "message": f"Calibration mode '{body.cali_mode.value}' is not implemented in this build yet.",
            },
        )
    if state == ModeState.PRODUCTION:
        raise HTTPException(
            409,
            detail={
                "detail": "mode_verification_not_applicable",
                "message": (
                    f"Calibration mode '{body.cali_mode.value}' is already in production — "
                    "use the standard wizard (POST /printers/{id}/calibration/sessions) "
                    "to enqueue a real print."
                ),
            },
        )

    if not await any_sidecar_online(db):
        raise HTTPException(
            409,
            detail={
                "detail": "slicer_sidecar_required",
                "message": (
                    "Calibration slicing requires a connected slicer sidecar; enable "
                    "'Server-side slicing' under Settings → General → General and configure "
                    "an OrcaSlicer / Bambu Studio API URL."
                ),
            },
        )

    # Bake the per-mode 3MF. Spec is passed through opaquely; per-mode
    # builders validate against the schemas in
    # backend/app/schemas/calibration_spec.py. We also fold body.bed_type
    # into spec so the builder can pin the 3MF's ``curr_bed_type``
    # (otherwise the scaffold's default "Cool Plate" fails PETG / TPU
    # plate-compat validation).
    spec_with_bed = dict(body.spec or {})
    if body.bed_type and "bed_type" not in spec_with_bed:
        spec_with_bed["bed_type"] = body.bed_type
    # Inject target printer's settings_id so the writer can patch the
    # scaffold's project_settings.config to match what's about to land
    # via --load-settings / bundle resolution. Otherwise BS CLI's
    # machine-switch guard (BambuStudio.cpp:2942) rejects with -16 when
    # the scaffold's hard-coded N1 identity disagrees with the bundle's
    # printer. Bundle path: name is right there in the body. PresetRef
    # path: deferred until cloud flattener lands (Option 1) — for now
    # the scaffold's N1 + cleared upward_compat list is permissive enough
    # for most cloud presets to slide through.
    if body.bundle is not None and "target_printer_settings_id" not in spec_with_bed:
        spec_with_bed["target_printer_settings_id"] = body.bundle.printer_name
    # Thread the slicer choice through so mode builders can swap
    # slicer-specific overrides (e.g. PA Tower's brim_type — Orca
    # supports brim_ears, BS does not and silently produces no brim).
    if body.slicer and "slicer" not in spec_with_bed:
        spec_with_bed["slicer"] = body.slicer

    # PA Line and Vol Speed Tower both need the printer's real bed
    # size. PA Line centres its pattern on the plate (absolute machine
    # coords); Vol Speed X-fits the tower to bed width. Both builders
    # default to 256 mm — fine for X1/A1/P1S, but on an A1 mini
    # (180 mm) PA Line ends up in the upper-right quadrant and the Vol
    # Speed tower slices ~246 mm wide and overflows the plate. Resolve
    # the printer preset once here and pass the bbox via spec; we
    # re-use the same JSON for the sidecar call below to avoid a double
    # round-trip. Bundle path: the printer JSON lives inside the
    # sidecar's bundle and isn't easily reachable without a separate
    # API call — falls back to the builder's 256 default.
    pre_resolved_printer_json: str | None = None
    if body.cali_mode in (CaliMode.PA_LINE, CaliMode.VOL_SPEED_TOWER):
        from backend.app.models.printer import Printer as PrinterModel
        from backend.app.services.calib_pa_line import (
            bed_bbox_for_model,
            parse_bed_bbox_from_printer_json,
        )

        bed_bbox: tuple[float, float, float, float] | None = None
        # Try the resolved cloud / local preset JSON first — it carries
        # ``printable_area`` when the operator picked a custom printer
        # preset that overrode the field.
        if body.bundle is None and body.printer_preset is not None:
            pre_resolved_printer_json = await resolve_preset_ref(db, user, body.printer_preset, "printer")
            bed_bbox = parse_bed_bbox_from_printer_json(pre_resolved_printer_json)
        # Cloud presets are deltas against ``base_id`` — ``printable_area``
        # almost always lives in the parent and isn't in the delta.
        # Fall back to the printer's registered model from our DB.
        if bed_bbox is None:
            printer_row = await db.get(PrinterModel, printer_id)
            bed_bbox = bed_bbox_for_model(printer_row.model if printer_row else None)
        if bed_bbox is not None:
            origin_x, origin_y, size_x, size_y = bed_bbox
            spec_with_bed.setdefault("bed_origin_x", origin_x)
            spec_with_bed.setdefault("bed_origin_y", origin_y)
            spec_with_bed.setdefault("bed_size_x", size_x)
            spec_with_bed.setdefault("bed_size_y", size_y)

    try:
        model_bytes = build_calibration_3mf(
            cali_mode=body.cali_mode,
            spec=spec_with_bed,
            extruder_count=body.extruder_count,
            pass_n=body.pass_n,
        )
    except NotImplementedError as exc:
        logger.warning("slice_only: builder not implemented for %s — %s", body.cali_mode.value, exc)
        raise HTTPException(409, detail={"detail": "mode_not_implemented", "message": str(exc)})
    except ValueError as exc:
        logger.warning("slice_only: builder rejected spec for %s — %s", body.cali_mode.value, exc)
        raise HTTPException(400, str(exc))

    _, api_url = await resolve_sidecar_url(db, slicer_override=body.slicer)
    if not api_url:
        raise HTTPException(503, "No slicer sidecar configured")

    model_filename = f"calibration_{body.cali_mode.value}.3mf"
    # Hoisted out of the manual branch so the Vol-Speed patcher can read
    # it regardless of which slice path (manual / bundle) ran.
    nozzle_diameter: float = float(
        (body.spec or {}).get("nozzle_diameter", 0.4) if isinstance(body.spec, dict) else 0.4
    )
    try:
        async with SlicerApiService(base_url=api_url) as svc:
            if body.bundle is not None:
                result = await svc.slice_with_bundle(
                    model_bytes=model_bytes,
                    model_filename=model_filename,
                    bundle_id=body.bundle.bundle_id,
                    printer_name=body.bundle.printer_name,
                    process_name=body.bundle.process_name,
                    filament_names=body.bundle.filament_names,
                    export_3mf=True,
                )
            else:
                # Manual path — resolve each PresetRef to the JSON the
                # sidecar's --load-settings expects. PA Line already
                # resolved the printer JSON above (to read printable_area
                # for pattern centring); reuse it instead of double-calling.
                printer_json = pre_resolved_printer_json or await resolve_preset_ref(
                    db, user, body.printer_preset, "printer"
                )
                process_json = await resolve_preset_ref(db, user, body.process_preset, "process")
                filament_jsons = [await resolve_preset_ref(db, user, ref, "filament") for ref in body.filament_presets]
                # Apply per-mode preset overrides — mirrors what
                # start_calibration does on the production path. Without
                # this the sidecar's --load-settings replays the
                # operator's preset values and overrides anything we'd
                # embedded into the 3MF's project_settings.config.
                from backend.app.services.calib_preset_overrides import (
                    apply_pa_line_filament_overrides,
                    apply_pa_line_printer_overrides,
                    apply_pa_line_process_overrides,
                    apply_pa_pattern_filament_overrides,
                    apply_pa_pattern_printer_overrides,
                    apply_pa_pattern_process_overrides,
                    apply_pa_tower_filament_overrides,
                    apply_pa_tower_process_overrides,
                    apply_retraction_printer_overrides,
                    apply_retraction_process_overrides,
                    apply_temp_filament_overrides,
                    apply_temp_printer_overrides,
                    apply_temp_process_overrides,
                    apply_vfa_filament_overrides,
                    apply_vfa_printer_overrides,
                    apply_vfa_process_overrides,
                    apply_vol_speed_filament_overrides,
                    apply_vol_speed_printer_overrides,
                    apply_vol_speed_process_overrides,
                )

                if body.cali_mode == CaliMode.PA_PATTERN:
                    process_json = apply_pa_pattern_process_overrides(process_json, nozzle_diameter=nozzle_diameter)
                    printer_json = apply_pa_pattern_printer_overrides(printer_json)
                    filament_jsons = [apply_pa_pattern_filament_overrides(f) for f in filament_jsons]
                elif body.cali_mode == CaliMode.PA_TOWER:
                    process_json = apply_pa_tower_process_overrides(process_json)
                    filament_jsons = [apply_pa_tower_filament_overrides(f) for f in filament_jsons]
                elif body.cali_mode == CaliMode.PA_LINE:
                    process_json = apply_pa_line_process_overrides(process_json, nozzle_diameter=nozzle_diameter)
                    printer_json = apply_pa_line_printer_overrides(printer_json)
                    filament_jsons = [apply_pa_line_filament_overrides(f) for f in filament_jsons]
                elif body.cali_mode == CaliMode.VOL_SPEED_TOWER:
                    process_json = apply_vol_speed_process_overrides(process_json)
                    printer_json = apply_vol_speed_printer_overrides(printer_json, nozzle_diameter=nozzle_diameter)
                    filament_jsons = [apply_vol_speed_filament_overrides(f) for f in filament_jsons]
                elif body.cali_mode == CaliMode.VFA_TOWER:
                    process_json = apply_vfa_process_overrides(process_json)
                    printer_json = apply_vfa_printer_overrides(printer_json)
                    filament_jsons = [apply_vfa_filament_overrides(f) for f in filament_jsons]
                elif body.cali_mode == CaliMode.TEMP_TOWER:
                    process_json = apply_temp_process_overrides(process_json)
                    printer_json = apply_temp_printer_overrides(printer_json)
                    _temp_start = int(round(float(spec_with_bed.get("start", 0))))
                    filament_jsons = [apply_temp_filament_overrides(f, start_temp=_temp_start) for f in filament_jsons]
                elif body.cali_mode == CaliMode.RETRACTION_TOWER:
                    process_json = apply_retraction_process_overrides(process_json)
                    printer_json = apply_retraction_printer_overrides(printer_json)

                # Log compat-relevant fields from each JSON so when BS rejects
                # the combo we can see what the resolver actually produced
                # instead of blindly trusting bundle / standard inherits.
                _log_preset_compat("printer", body.printer_preset, printer_json)
                _log_preset_compat("process", body.process_preset, process_json)
                for ref, j in zip(body.filament_presets, filament_jsons, strict=True):
                    _log_preset_compat("filament", ref, j)
                result = await svc.slice_with_profiles(
                    model_bytes=model_bytes,
                    model_filename=model_filename,
                    printer_profile_json=printer_json,
                    process_profile_json=process_json,
                    filament_profile_jsons=filament_jsons,
                    export_3mf=True,
                    bed_type=body.bed_type,
                )
    except SlicerInputError as exc:
        # Sidecar 4xx — the CLI rejected our input (incompat preset,
        # malformed 3MF, missing required key). Full message includes
        # CLI stderr — log to backend console so the operator doesn't
        # have to copy from a frontend toast.
        logger.error(
            "slice_only: sidecar rejected input (mode=%s printer=%s url=%s): %s",
            body.cali_mode.value,
            body.bundle.printer_name if body.bundle else (body.printer_preset.id if body.printer_preset else "?"),
            api_url,
            exc,
        )
        raise HTTPException(400, f"Slicer rejected input: {exc}") from exc
    except SlicerApiUnavailableError as exc:
        logger.error("slice_only: sidecar unreachable at %s: %s", api_url, exc)
        raise HTTPException(503, str(exc)) from exc
    except SlicerApiError as exc:
        logger.error("slice_only: sidecar error at %s (mode=%s): %s", api_url, body.cali_mode.value, exc)
        raise HTTPException(502, str(exc)) from exc

    # Tower modes: the sidecar can't apply the per-layer ramp
    # (Calib_*_Tower are GUI-only Print flags, never carried in the 3MF) —
    # rewrite the outer-wall feedrate (Vol Speed / VFA) or insert the M104
    # temperature ramp (Temp) ourselves; see calib_speed_ramp_patcher.
    sliced_content = result.content
    if body.cali_mode in (
        CaliMode.VOL_SPEED_TOWER,
        CaliMode.VFA_TOWER,
        CaliMode.TEMP_TOWER,
        CaliMode.RETRACTION_TOWER,
    ):
        from backend.app.services.calib_speed_ramp_patcher import (
            patch_retraction_tower,
            patch_temp_tower,
            patch_vfa_ramp,
            patch_vol_speed_ramp,
        )

        _spec = body.spec if isinstance(body.spec, dict) else {}
        try:
            if body.cali_mode == CaliMode.VOL_SPEED_TOWER:
                sliced_content = patch_vol_speed_ramp(
                    sliced_content,
                    start=float(_spec["start"]),
                    step=float(_spec["step"]),
                    nozzle_diameter=nozzle_diameter,
                )
            elif body.cali_mode == CaliMode.VFA_TOWER:
                sliced_content = patch_vfa_ramp(
                    sliced_content,
                    start=float(_spec["start"]),
                    step=float(_spec["step"]),
                )
            elif body.cali_mode == CaliMode.RETRACTION_TOWER:
                sliced_content = patch_retraction_tower(
                    sliced_content,
                    start=float(_spec["start"]),
                    step=float(_spec["step"]),
                )
            else:
                sliced_content = patch_temp_tower(sliced_content, start=float(_spec["start"]))
        except (KeyError, ValueError) as exc:
            logger.error("slice_only: tower ramp patch failed (mode=%s): %s", body.cali_mode.value, exc)
            raise HTTPException(500, f"Tower ramp patch failed: {exc}") from exc

    # Replace the slicer-generated placeholder-cube preview with our
    # branded "PA Test" thumbnail before the operator downloads it (so
    # the BS / Orca preview pane shows "PA Test" instead of a 3 mm
    # corner cube). Mirrors what the production-dispatch path does
    # before persisting to LibraryFile.
    from backend.app.services.calib_thumbnail import apply_calibration_thumbnail

    patched_content = apply_calibration_thumbnail(sliced_content, body.cali_mode)

    await _audit(
        db,
        printer_id=printer_id,
        user=user,
        action="slice_only",
        payload={
            **body.model_dump(),
            "bytes_in": len(model_bytes),
            "bytes_out": len(patched_content),
            "print_time_seconds": result.print_time_seconds,
            "filament_used_g": result.filament_used_g,
        },
    )

    download_name = f"calibration_{body.cali_mode.value}.gcode.3mf"
    quoted = urllib.parse.quote(download_name)
    return Response(
        content=patched_content,
        media_type="model/3mf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "X-Print-Time-Seconds": str(result.print_time_seconds),
            "X-Filament-Used-G": str(result.filament_used_g),
        },
    )


@router.post(
    "/printers/{printer_id}/calibration/bake-only",
    responses={
        200: {
            "content": {"model/3mf": {}},
            "description": "Composed calibration .3mf as an HTTP attachment.",
        },
        409: {"description": "Mode is not in VERIFICATION state."},
    },
)
async def bake_calibration_3mf(
    printer_id: int,
    body: CalibBakeOnlyIn = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> Response:
    """Bake the calibration 3MF and return it without slicing.

    Companion to ``slice-only``: same MODE_STATE gate, same per-mode
    builder, same ``CalibrationSpec`` shape, but stops before
    ``SlicerApiService`` is invoked. Operator gets the raw composed
    ``.3mf`` so they can unzip it and inspect what BamDude actually
    hands the sidecar — useful when sign-off diffs need to attribute
    a discrepancy to either the bake step (BamDude) or the slice step
    (BS / Orca).
    """
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    state = get_mode_state(body.cali_mode)
    if state == ModeState.DISABLED:
        await _audit(
            db,
            printer_id=printer_id,
            user=user,
            action="bake_only",
            payload=body.model_dump(),
            result="error",
            error=f"mode_not_implemented:{body.cali_mode.value}",
        )
        raise HTTPException(
            409,
            detail={
                "detail": "mode_not_implemented",
                "message": f"Calibration mode '{body.cali_mode.value}' is not implemented in this build yet.",
            },
        )
    # PRODUCTION-state modes also fall through here — there's no harm in
    # letting the operator pull the baked 3MF for debugging once a mode
    # is already wired to dispatch. No verification-only gate.

    spec_with_bed = dict(body.spec or {})
    if body.bed_type and "bed_type" not in spec_with_bed:
        spec_with_bed["bed_type"] = body.bed_type
    # No target_printer_settings_id branch here: ``CalibBakeOnlyIn`` has
    # no ``bundle`` / preset fields. The bake-only artefact is for
    # operator inspection only — nothing slices it through --load-settings,
    # so the scaffold's N1 identity stays in place without consequence.
    try:
        model_bytes = build_calibration_3mf(
            cali_mode=body.cali_mode,
            spec=spec_with_bed,
            extruder_count=body.extruder_count,
            pass_n=body.pass_n,
        )
    except NotImplementedError as exc:
        logger.warning("bake_only: builder not implemented for %s — %s", body.cali_mode.value, exc)
        raise HTTPException(409, detail={"detail": "mode_not_implemented", "message": str(exc)})
    except ValueError as exc:
        logger.warning("bake_only: builder rejected spec for %s — %s", body.cali_mode.value, exc)
        raise HTTPException(400, str(exc))

    # Bake-only returns the pre-slice scaffold 3MF — its embedded
    # thumbnail is still the scaffold's placeholder-cube render. Patch
    # it the same way the sliced paths do so the operator's BS / Orca
    # preview shows "PA Test" branding even for the debug bake.
    from backend.app.services.calib_thumbnail import apply_calibration_thumbnail

    model_bytes = apply_calibration_thumbnail(model_bytes, body.cali_mode)

    await _audit(
        db,
        printer_id=printer_id,
        user=user,
        action="bake_only",
        payload={**body.model_dump(), "bytes_out": len(model_bytes)},
    )

    download_name = f"calibration_{body.cali_mode.value}.bake.3mf"
    quoted = urllib.parse.quote(download_name)
    return Response(
        content=model_bytes,
        media_type="model/3mf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


# ---------- filament_calibration CRUD ----------


@router.get(
    "/filament-calibrations",
    response_model=list[FilamentCalibrationOut],
)
async def list_filament_calibrations(
    printer_id: int | None = Query(default=None),
    filament_id: str | None = Query(default=None),
    nozzle_diameter: float | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[FilamentCalibrationOut]:
    q = select(FilamentCalibration)
    if printer_id is not None:
        q = q.where(FilamentCalibration.printer_id == printer_id)
    if filament_id:
        q = q.where(FilamentCalibration.filament_id == filament_id)
    if nozzle_diameter is not None:
        q = q.where(FilamentCalibration.nozzle_diameter == nozzle_diameter)
    if is_active is not None:
        q = q.where(FilamentCalibration.is_active.is_(is_active))
    q = q.order_by(FilamentCalibration.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    return [FilamentCalibrationOut.model_validate(r) for r in rows]


@router.get(
    "/filament-calibrations/{cali_id}",
    response_model=FilamentCalibrationOut,
)
async def get_filament_calibration(
    cali_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> FilamentCalibrationOut:
    row = (await db.execute(select(FilamentCalibration).where(FilamentCalibration.id == cali_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Calibration not found")
    return FilamentCalibrationOut.model_validate(row)


@router.post(
    "/filament-calibrations/{cali_id}/set-active",
    response_model=FilamentCalibrationOut,
)
async def set_active_calibration(
    cali_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> FilamentCalibrationOut:
    row = (await db.execute(select(FilamentCalibration).where(FilamentCalibration.id == cali_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Calibration not found")

    # Flip combo siblings is_active → False before flipping ours → True so the
    # partial unique index sees only one active row at a time.
    siblings = (
        (
            await db.execute(
                select(FilamentCalibration).where(
                    FilamentCalibration.printer_id == row.printer_id,
                    FilamentCalibration.filament_id == row.filament_id,
                    FilamentCalibration.nozzle_diameter == row.nozzle_diameter,
                    FilamentCalibration.nozzle_volume_type == row.nozzle_volume_type,
                    FilamentCalibration.extruder_id == row.extruder_id,
                    FilamentCalibration.is_active.is_(True),
                    FilamentCalibration.id != row.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for sib in siblings:
        sib.is_active = False
    if siblings:
        await db.commit()
    row.is_active = True
    await db.commit()
    await db.refresh(row)

    await _audit(
        db,
        printer_id=row.printer_id,
        user=user,
        action="set_active",
        payload={"cali_id": cali_id},
        filament_calibration_id=row.id,
    )
    return FilamentCalibrationOut.model_validate(row)


@router.delete("/filament-calibrations/{cali_id}", status_code=204)
async def delete_calibration(
    cali_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRINTERS_UPDATE),
) -> None:
    row = (await db.execute(select(FilamentCalibration).where(FilamentCalibration.id == cali_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Calibration not found")
    printer_id = row.printer_id
    audit_payload = {"cali_id": cali_id}
    await db.delete(row)
    await db.commit()
    await _audit(
        db,
        printer_id=printer_id,
        user=user,
        action="delete",
        payload=audit_payload,
    )


# ---------- Printer-side history ----------


@router.get(
    "/printers/{printer_id}/calibration/history",
    response_model=list[PACalibHistoryEntryOut],
)
async def get_history(
    printer_id: int,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[PACalibHistoryEntryOut]:
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    return [
        PACalibHistoryEntryOut(
            cali_idx=h.cali_idx,
            name=h.name,
            filament_id=h.filament_id,
            setting_id=h.setting_id,
            nozzle_diameter=h.nozzle_diameter,
            nozzle_volume_type=h.nozzle_volume_type,
            extruder_id=h.extruder_id,
            k_value=h.k_value,
            n_coef=h.n_coef,
        )
        for h in (client.state.extrusion_cali_history or [])
    ]


@router.get(
    "/printers/{printer_id}/calibration/auto-results",
    response_model=list[dict],
)
async def get_auto_results(
    printer_id: int,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> list[dict]:
    """Drain PrinterState.extrusion_cali_results for the X1 auto-cali save UI.

    Each row pairs an AMS slot with the K (or flow ratio — firmware reuses
    the same payload slot) the lidar measured. UI lets the operator pick /
    edit / skip per row before saving.
    """
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    return [
        {
            "tray_id": r.tray_id,
            "ams_id": r.ams_id,
            "slot_id": r.slot_id,
            "extruder_id": r.extruder_id,
            "nozzle_diameter": r.nozzle_diameter,
            "nozzle_volume_type": r.nozzle_volume_type,
            "filament_id": r.filament_id,
            "setting_id": r.setting_id,
            "k_value": r.k_value,
            "n_coef": r.n_coef,
            "confidence": r.confidence,
            "nozzle_pos_id": r.nozzle_pos_id,
            "nozzle_sn": r.nozzle_sn,
        }
        for r in (client.state.extrusion_cali_results or [])
    ]


@router.post(
    "/printers/{printer_id}/calibration/history/refresh",
    status_code=202,
)
async def refresh_history(
    printer_id: int,
    nozzle_diameter: float = Query(default=0.4),
    extruder_id: int = Query(default=0),
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
) -> dict:
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        raise HTTPException(404, "Printer not online")
    ok, seq = client.extrusion_cali_query_history(
        nozzle_diameter=nozzle_diameter,
        extruder_id=extruder_id,
    )
    if not ok:
        raise HTTPException(504, "MQTT publish failed")
    return {"sequence_id": seq}
