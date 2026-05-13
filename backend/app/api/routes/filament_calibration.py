"""REST routes for the Filament Calibration wizard (m062 / Plan 1).

Mounted at /printers and /calibration prefixes via the single router below;
permission PRINTERS_UPDATE gates POST/DELETE, PRINTERS_READ gates GETs.
Every mutation also writes a calibration_audit row mirroring m060 + m061
audit-trail patterns.
"""

from __future__ import annotations

import json as _json

from fastapi import APIRouter, Body, Depends, HTTPException, Query
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
    CalibCapabilities,
    CalibrationSessionOut,
    FilamentCalibrationOut,
    ManualResultIn,
    ManualResultOutSchema,
    PACalibHistoryEntryOut,
    StartSessionIn,
)
from backend.app.services.calibration_service import (
    CalibFilamentInput,
    CalibrationService,
    SlicerSidecarRequiredError,
    reconcile_session_status,
)
from backend.app.services.printer_capabilities import compute_calibration_supports
from backend.app.services.printer_manager import printer_manager

router = APIRouter(tags=["filament-calibration"])
_service = CalibrationService()


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
            coarse_modifier=body.coarse_modifier,
            skip_fine=body.skip_fine,
            fine_modifier=body.fine_modifier,
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
