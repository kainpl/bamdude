"""Orchestrator for the Filament Calibration wizard (m062 / Plan 1).

Two main paths:
    AUTO (X1 / X1E / H2D Pro with lidar): MQTT extrusion_cali mode=0,
        printer prints + scans + pushes back results via
        extrusion_cali_get_result. UI drains state.extrusion_cali_results.
    MANUAL (all): copy 3MF asset from data/calib_assets/ → enqueue through
        background_dispatch as an is_calibration=True PrintQueueItem.

Save flow auto-binds to the AMS slot via extrusion_cali_sel so subsequent
prints (BamDude / BS / printer screen) use the new value. Dispatch hook
re-sels before each non-cali print as belt-and-suspenders sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.websocket import ws_manager
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.printer import Printer
from backend.app.services.calibration_constants import (
    CaliMethod,
    CaliMode,
    NozzleVolumeType,
    compute_flow_ratio_coarse,
    compute_flow_ratio_fine,
    compute_pa_k,
    generate_nozzle_id,
)
from backend.app.services.printer_manager import printer_manager

ASSET_ROOT = Path(__file__).resolve().parent.parent / "data" / "calib_assets"

# Tower modes are print-and-finish — no save dialog, no filament_calibration
# row. The dispatch on-complete handler flips the session straight to
# "saved" instead of "awaiting_user_input".
TOWER_MODES = frozenset(
    {
        CaliMode.TEMP_TOWER,
        CaliMode.VOL_SPEED_TOWER,
        CaliMode.VFA_TOWER,
        CaliMode.RETRACTION_TOWER,
    }
)


def is_tower_mode(cali_mode: str | CaliMode) -> bool:
    if isinstance(cali_mode, str):
        try:
            cali_mode = CaliMode(cali_mode)
        except ValueError:
            return False
    return cali_mode in TOWER_MODES


async def broadcast_calibration_event(*, printer_id: int, event: str, payload: dict | None = None) -> None:
    """Wrap ws_manager.broadcast with a ``calibration.<event>`` envelope.

    Frontend ``useWebSocket`` routes ``calibration.*`` messages to a
    CustomEvent + TanStack Query invalidation; the wizard hook listens
    for ``calibration-event`` and advances its step machine. Emission is
    best-effort — never break the persistence path on WS failure.
    """
    try:
        await ws_manager.broadcast(
            {
                "type": f"calibration.{event}",
                "printer_id": printer_id,
                "data": payload or {},
            }
        )
    except Exception:
        pass


_MODE_TO_PATH = {
    CaliMode.PA_LINE: ("pressure_advance", "pa_line"),
    CaliMode.PA_PATTERN: ("pressure_advance", "pa_pattern"),
    CaliMode.PA_TOWER: ("pressure_advance", "pa_tower"),
    CaliMode.TEMP_TOWER: ("temp_tower", "temp_tower"),
    CaliMode.VOL_SPEED_TOWER: ("volumetric_speed", "vol_speed_tower"),
    CaliMode.VFA_TOWER: ("vfa", "vfa_tower"),
    CaliMode.RETRACTION_TOWER: ("retraction", "retraction_tower"),
}


def resolve_asset_path(cali_mode: CaliMode, *, nozzle_diameter: float, pass_n: int = 1) -> Path:
    """Map (cali_mode, diameter) → 3MF asset path. Falls back to 0.4mm if a
    diameter-specific variant is missing.
    """
    if cali_mode == CaliMode.FLOW_RATE:
        fname = f"flowrate_pass{pass_n}_{nozzle_diameter}.3mf"
        path = ASSET_ROOT / "filament_flow" / fname
        if not path.exists():
            path = ASSET_ROOT / "filament_flow" / f"flowrate_pass{pass_n}_0.4.3mf"
        return path

    bucket = _MODE_TO_PATH.get(cali_mode)
    if bucket is None:
        raise ValueError(f"No asset mapping for cali_mode: {cali_mode}")
    subdir, stem = bucket
    path = ASSET_ROOT / subdir / f"{stem}_{nozzle_diameter}.3mf"
    if not path.exists():
        path = ASSET_ROOT / subdir / f"{stem}_0.4.3mf"
    return path


@dataclass
class CalibFilamentInput:
    """Per-filament input to start_calibration. Survives the API hop via
    backend.app.schemas.filament_calibration.CalibFilamentIn → service.
    """

    ams_id: int
    slot_id: int
    tray_id: int
    filament_id: str
    filament_setting_id: str | None
    bed_temp: int
    nozzle_temp: int
    max_volumetric_speed: float
    flow_rate: float = 0.98
    extruder_id_override: int | None = None


@dataclass
class ResultPayload:
    """Internal save_result input — what the persistence layer writes."""

    pa_k_value: float | None = None
    pa_n_coef: float | None = None
    flow_ratio: float | None = None
    confidence: int | None = None
    cali_idx: int | None = None
    source: str = "manual"
    name: str = ""


@dataclass
class ManualResultOut:
    """Return shape of submit_manual_result."""

    saved_rows: list[FilamentCalibration] = field(default_factory=list)
    next_session_id: int | None = None


class CalibrationService:
    """Stateless orchestrator — all state lives in DB + PrinterState.

    All methods take an explicit AsyncSession to play nice with FastAPI's
    request-scoped DI. printer_manager.get_client() resolves the live MQTT
    handle.
    """

    async def start_calibration(
        self,
        *,
        db: AsyncSession,
        printer_id: int,
        cali_mode: CaliMode,
        method: CaliMethod,
        nozzle_diameter: float,
        nozzle_volume_type: str,
        extruder_id: int,
        filaments: list[CalibFilamentInput],
        user_id: int | None,
    ) -> CalibrationSession:
        # Concurrent guard — one active session per printer
        existing = (
            await db.execute(
                select(CalibrationSession).where(
                    CalibrationSession.printer_id == printer_id,
                    CalibrationSession.status.in_(["running", "awaiting_user_input"]),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"active_session_exists:{existing.id}")

        client = printer_manager.get_client(printer_id)
        if not client or not client.state.connected:
            raise ValueError("Printer not online")

        # Build BS-shape filaments payload — used for both AUTO and MANUAL
        nozzle_id = generate_nozzle_id(NozzleVolumeType(nozzle_volume_type), nozzle_diameter)
        filaments_payload = [
            {
                "tray_id": f.tray_id,
                "extruder_id": f.extruder_id_override if f.extruder_id_override is not None else extruder_id,
                "bed_temp": f.bed_temp,
                "filament_id": f.filament_id,
                "setting_id": f.filament_setting_id or "",
                "nozzle_temp": f.nozzle_temp,
                "ams_id": f.ams_id,
                "slot_id": f.slot_id,
                "nozzle_id": nozzle_id,
                "nozzle_diameter": str(nozzle_diameter),
                "max_volumetric_speed": str(f.max_volumetric_speed),
            }
            for f in filaments
        ]

        sequence_id: str | None = None
        print_queue_item_id: int | None = None

        if method == CaliMethod.AUTO and cali_mode == CaliMode.AUTO_PA_LINE:
            ok, sequence_id = client.extrusion_cali_start(
                nozzle_diameter=nozzle_diameter,
                cali_mode=0,
                filaments=filaments_payload,
            )
            if not ok:
                raise ValueError("MQTT publish failed")
        elif method == CaliMethod.AUTO and cali_mode == CaliMode.FLOW_RATE:
            for fp, f in zip(filaments_payload, filaments, strict=True):
                fp["flow_rate"] = f.flow_rate
            ok, sequence_id = client.flow_rate_cali_start(
                nozzle_diameter=nozzle_diameter,
                filaments=filaments_payload,
            )
            if not ok:
                raise ValueError("MQTT publish failed")
        else:
            # MANUAL path: resolve 3MF asset → enqueue as is_calibration print
            from backend.app.services import background_dispatch  # late import to dodge cycle

            asset_path = resolve_asset_path(cali_mode, nozzle_diameter=nozzle_diameter, pass_n=1)
            if not asset_path.exists():
                raise ValueError(f"calibration asset not available: {asset_path.name}")
            if not filaments:
                raise ValueError("manual calibration needs at least one filament")
            print_queue_item_id = await background_dispatch.enqueue_calibration_print(
                printer_id=printer_id,
                asset_path=str(asset_path),
                cali_mode=cali_mode.value,
                user_id=user_id,
                ams_id=filaments[0].ams_id,
                slot_id=filaments[0].slot_id,
                tray_id=filaments[0].tray_id,
            )

        session = CalibrationSession(
            printer_id=printer_id,
            user_id=user_id,
            cali_mode=cali_mode.value,
            method=method.value,
            nozzle_diameter=nozzle_diameter,
            nozzle_volume_type=nozzle_volume_type,
            extruder_id=extruder_id,
            filaments_json=json.dumps(filaments_payload),
            status="running",
            mqtt_sequence_id=sequence_id,
            stage=1,
            print_queue_item_id=print_queue_item_id,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        await broadcast_calibration_event(
            printer_id=printer_id,
            event="started",
            payload={"session_id": session.id, "cali_mode": cali_mode.value, "method": method.value},
        )
        return session

    async def submit_manual_result(
        self,
        *,
        db: AsyncSession,
        session_id: int,
        best_line_index: int | None = None,
        coarse_modifier: int | None = None,
        skip_fine: bool = False,
        fine_modifier: int | None = None,
    ) -> ManualResultOut:
        s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one()
        if s.status != "awaiting_user_input":
            raise ValueError(f"session not awaiting input (status={s.status})")

        cm = CaliMode(s.cali_mode)

        if cm in (CaliMode.PA_LINE, CaliMode.PA_PATTERN, CaliMode.PA_TOWER):
            if best_line_index is None:
                raise ValueError("best_line_index required for PA mode")
            k = compute_pa_k(best_line_index)
            row = await self.save_result(
                db=db,
                session=s,
                payload=ResultPayload(
                    pa_k_value=k,
                    source="manual",
                    name=f"{s.cali_mode} K={k:.4f}",
                ),
            )
            return ManualResultOut(saved_rows=[row])

        if cm == CaliMode.FLOW_RATE and s.stage == 1:
            if coarse_modifier is None:
                raise ValueError("coarse_modifier required for Flow Rate stage 1")
            coarse = compute_flow_ratio_coarse(coarse_modifier)
            s.coarse_ratio = coarse
            await db.commit()
            if skip_fine:
                row = await self.save_result(
                    db=db,
                    session=s,
                    payload=ResultPayload(
                        flow_ratio=coarse,
                        source="manual",
                        name=f"flow_rate {coarse:.3f} (coarse only)",
                    ),
                )
                return ManualResultOut(saved_rows=[row])
            stage2 = await self._start_flow_rate_stage2(db=db, parent=s)
            return ManualResultOut(next_session_id=stage2.id)

        if cm == CaliMode.FLOW_RATE and s.stage == 2:
            if fine_modifier is None:
                raise ValueError("fine_modifier required for Flow Rate stage 2")
            if s.coarse_ratio is None:
                raise ValueError("stage-2 session missing coarse_ratio")
            fine = compute_flow_ratio_fine(s.coarse_ratio, fine_modifier)
            row = await self.save_result(
                db=db,
                session=s,
                payload=ResultPayload(
                    flow_ratio=fine,
                    source="manual",
                    name=f"flow_rate {fine:.3f}",
                ),
            )
            return ManualResultOut(saved_rows=[row])

        raise ValueError(f"submit_manual_result unsupported for mode {cm}")

    async def _start_flow_rate_stage2(
        self,
        *,
        db: AsyncSession,
        parent: CalibrationSession,
    ) -> CalibrationSession:
        """Create stage-2 session inheriting parent's filament selection.

        Phase-1 wires the row only; subsequent print-asset dispatch happens
        via the same background_dispatch.enqueue_calibration_print pipe when
        upstream wiring lands in Wave 5.
        """
        stage2 = CalibrationSession(
            printer_id=parent.printer_id,
            user_id=parent.user_id,
            cali_mode=parent.cali_mode,
            method=parent.method,
            nozzle_diameter=parent.nozzle_diameter,
            nozzle_volume_type=parent.nozzle_volume_type,
            extruder_id=parent.extruder_id,
            filaments_json=parent.filaments_json,
            status="awaiting_user_input",
            stage=2,
            parent_session_id=parent.id,
            coarse_ratio=parent.coarse_ratio,
        )
        db.add(stage2)
        await db.commit()
        await db.refresh(stage2)
        return stage2

    async def submit_auto_result(
        self,
        *,
        db: AsyncSession,
        session_id: int,
        edits: list[dict],
    ) -> list[FilamentCalibration]:
        s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one()
        if s.status != "awaiting_user_input":
            raise ValueError(f"session not awaiting input (status={s.status})")

        client = printer_manager.get_client(s.printer_id)
        if not client:
            raise ValueError("Printer not online")

        # X1 auto-flow delivers the flow ratio via the same push slot as PA's
        # k_value — the firmware reuses the field. Branch by session.cali_mode
        # so each row lands in the right column (pa_k_value vs flow_ratio).
        is_flow = CaliMode(s.cali_mode) == CaliMode.FLOW_RATE
        results_by_tray = {r.tray_id: r for r in client.state.extrusion_cali_results}
        saved: list[FilamentCalibration] = []
        for edit in edits:
            if not edit.get("save", True):
                continue
            base = results_by_tray.get(edit["tray_id"])
            if base is None:
                continue
            # edit.get(...) returns the value even when it's None — fall back
            # to base.* explicitly so user-omitted fields don't blow up float().
            if is_flow:
                flow_raw = edit.get("flow_ratio")
                flow = float(flow_raw if flow_raw is not None else base.k_value)
                name = edit.get("name") or f"{base.filament_id} flow {flow:.3f}"
                row = await self.save_result(
                    db=db,
                    session=s,
                    payload=ResultPayload(
                        flow_ratio=flow,
                        confidence=base.confidence,
                        source="auto",
                        name=name,
                    ),
                )
            else:
                k_raw = edit.get("k_value")
                k = float(k_raw if k_raw is not None else base.k_value)
                n_raw = edit.get("n_coef")
                n = float(n_raw if n_raw is not None else base.n_coef)
                name = edit.get("name") or f"{base.filament_id} PA {k:.4f}"
                row = await self.save_result(
                    db=db,
                    session=s,
                    payload=ResultPayload(
                        pa_k_value=k,
                        pa_n_coef=n,
                        confidence=base.confidence,
                        source="auto",
                        name=name,
                    ),
                )
            saved.append(row)
        return saved

    async def save_result(
        self,
        *,
        db: AsyncSession,
        session: CalibrationSession,
        payload: ResultPayload,
    ) -> FilamentCalibration:
        """Persist a calibration result row, push to printer history, auto-bind.

        Side effects:
          1. UPDATE existing is_active rows for the combo → False (preserves history).
          2. INSERT new row with is_active=True.
          3. MQTT extrusion_cali_set → printer-side 16-slot history.
          4. MQTT extrusion_cali_sel → bind to AMS slot (so subsequent prints use it).
          5. session.status='saved'.
        """
        printer = (await db.execute(select(Printer).where(Printer.id == session.printer_id))).scalar_one()
        fil = json.loads(session.filaments_json)[0]

        # Flip existing active rows to false (preserves history)
        existing = (
            (
                await db.execute(
                    select(FilamentCalibration).where(
                        FilamentCalibration.printer_model == printer.model,
                        FilamentCalibration.filament_id == fil["filament_id"],
                        FilamentCalibration.nozzle_diameter == session.nozzle_diameter,
                        FilamentCalibration.nozzle_volume_type == session.nozzle_volume_type,
                        FilamentCalibration.extruder_id == session.extruder_id,
                        FilamentCalibration.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            row.is_active = False
        # Commit the flip BEFORE the insert so the partial unique index sees
        # only one is_active row at a time.
        if existing:
            await db.commit()

        new_row = FilamentCalibration(
            printer_model=printer.model,
            filament_id=fil["filament_id"],
            filament_setting_id=fil.get("setting_id") or None,
            nozzle_diameter=session.nozzle_diameter,
            nozzle_volume_type=session.nozzle_volume_type,
            extruder_id=session.extruder_id,
            pa_k_value=payload.pa_k_value,
            pa_n_coef=payload.pa_n_coef,
            flow_ratio=payload.flow_ratio,
            confidence=payload.confidence,
            cali_mode=session.cali_mode,
            source=payload.source,
            is_active=True,
            cali_idx=payload.cali_idx,
            name=payload.name or f"{fil['filament_id']} cali",
            calibrated_on_printer_id=session.printer_id,
            calibrated_by_user_id=session.user_id,
        )
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)

        # MQTT push to printer history + auto-bind. BS uses the same
        # extrusion_cali_set verb for both PA and flow ratio — firmware
        # routes by session context. Push whichever value we computed.
        push_value = payload.pa_k_value if payload.pa_k_value is not None else payload.flow_ratio
        client = printer_manager.get_client(session.printer_id)
        if client and client.state.connected and push_value is not None:
            client.extrusion_cali_set(
                tray_id=fil["tray_id"],
                k_value=push_value,
                nozzle_diameter=str(session.nozzle_diameter),
                nozzle_temp=fil.get("nozzle_temp", 220),
                filament_id=fil["filament_id"],
                setting_id=fil.get("setting_id") or "",
                name=payload.name,
                cali_idx=new_row.cali_idx or -1,
            )
            if new_row.cali_idx is not None:
                client.extrusion_cali_sel(
                    ams_id=fil["ams_id"],
                    tray_id=fil["tray_id"],
                    cali_idx=new_row.cali_idx,
                    filament_id=fil["filament_id"],
                    nozzle_diameter=str(session.nozzle_diameter),
                )

        session.status = "saved"
        await db.commit()
        await broadcast_calibration_event(
            printer_id=session.printer_id,
            event="saved",
            payload={"session_id": session.id, "filament_calibration_id": new_row.id},
        )
        return new_row

    async def cancel_session(self, *, db: AsyncSession, session_id: int) -> None:
        """Cancel an in-flight session.

        - status=running auto: MQTT print.command='stop'.
        - status=running manual w/ print active: same.
        - status=awaiting_user_input: just mark cancelled.
        """
        s = (await db.execute(select(CalibrationSession).where(CalibrationSession.id == session_id))).scalar_one()
        if s.status in ("saved", "cancelled", "failed"):
            return

        client = printer_manager.get_client(s.printer_id)
        if s.status == "running" and client:
            stop_fn = getattr(client, "stop_print", None)
            if callable(stop_fn):
                stop_fn()
        s.status = "cancelled"
        await db.commit()
        await broadcast_calibration_event(printer_id=s.printer_id, event="cancelled", payload={"session_id": s.id})


async def reconcile_session_status(db: AsyncSession, session: CalibrationSession) -> bool:
    """Lazy-flip running → awaiting_user_input | saved | failed.

    Auto path: PrinterState.extrusion_cali_status reports "completed" once
    the printer pushes extrusion_cali_get_result → flip to
    awaiting_user_input.

    Manual path: linked PrintQueueItem reaches "completed" / "failed" →
    flip to awaiting_user_input (tower modes go straight to "saved").

    Returns True if status changed. Best-effort; never raises.
    """
    if session.status != "running":
        return False
    try:
        client = printer_manager.get_client(session.printer_id)
        new_status: str | None = None
        if session.method == "auto":
            if client and getattr(client.state, "extrusion_cali_status", "idle") == "completed":
                new_status = "saved" if is_tower_mode(session.cali_mode) else "awaiting_user_input"
        elif session.print_queue_item_id is not None:
            from backend.app.models.print_queue import PrintQueueItem  # local import

            item = await db.get(PrintQueueItem, session.print_queue_item_id)
            if item and item.status in {"completed", "failed"}:
                if item.status == "failed":
                    new_status = "failed"
                else:
                    new_status = "saved" if is_tower_mode(session.cali_mode) else "awaiting_user_input"

        if new_status and new_status != session.status:
            session.status = new_status
            await db.commit()
            await broadcast_calibration_event(
                printer_id=session.printer_id,
                event="saved" if new_status == "saved" else ("failed" if new_status == "failed" else "completed"),
                payload={"session_id": session.id},
            )
            return True
    except Exception:
        return False
    return False


async def resolve_active_calibration(
    *,
    db: AsyncSession,
    printer_model: str,
    filament_id: str,
    nozzle_dia: float,
    nozzle_vol_type: str,
    extruder_id: int,
) -> FilamentCalibration | None:
    """Pure SELECT for the dispatch hook. Returns active row for the combo,
    or None if no calibration exists.
    """
    return (
        await db.execute(
            select(FilamentCalibration).where(
                FilamentCalibration.printer_model == printer_model,
                FilamentCalibration.filament_id == filament_id,
                FilamentCalibration.nozzle_diameter == nozzle_dia,
                FilamentCalibration.nozzle_volume_type == nozzle_vol_type,
                FilamentCalibration.extruder_id == extruder_id,
                FilamentCalibration.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
