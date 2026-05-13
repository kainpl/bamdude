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
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.websocket import ws_manager
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.filament_calibration import FilamentCalibration
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

logger = logging.getLogger(__name__)

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
        fil = json.loads(session.filaments_json)[0]

        # Flip existing active rows to false (preserves history). Scope is
        # per-printer (m063) — calibrations don't share across instances.
        existing = (
            (
                await db.execute(
                    select(FilamentCalibration).where(
                        FilamentCalibration.printer_id == session.printer_id,
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
            printer_id=session.printer_id,
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
            nozzle_id=generate_nozzle_id(NozzleVolumeType(session.nozzle_volume_type), session.nozzle_diameter),
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
                cali_idx=new_row.cali_idx if new_row.cali_idx is not None else -1,
            )

            # Q3 fix: MANUAL path doesn't ship cali_idx — printer auto-assigns
            # one when extrusion_cali_set arrives with cali_idx=-1. Round-trip
            # extrusion_cali_get to discover the assigned index, then bind.
            # AUTO path already has cali_idx in PACalibResult so this is a
            # no-op there.
            if new_row.cali_idx is None:
                try:
                    entries = await client.get_kprofiles(str(session.nozzle_diameter))
                    for kp in entries or []:
                        if (
                            kp.filament_id == fil["filament_id"]
                            and kp.name == new_row.name
                            and abs(float(kp.k_value or 0) - float(push_value or 0)) < 1e-6
                        ):
                            new_row.cali_idx = int(kp.slot_id)
                            await db.commit()
                            break
                except Exception as e:
                    logger.warning("save_result: cali_idx round-trip failed: %s", e)

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
    printer_id: int,
    filament_id: str,
    nozzle_dia: float,
    nozzle_vol_type: str,
    extruder_id: int,
) -> FilamentCalibration | None:
    """Pure SELECT for the dispatch hook. Returns active row for the combo,
    or None if no calibration exists.

    Scope is per-printer-instance (m063) — two X1Cs in a farm don't share
    calibration rows.
    """
    return (
        await db.execute(
            select(FilamentCalibration).where(
                FilamentCalibration.printer_id == printer_id,
                FilamentCalibration.filament_id == filament_id,
                FilamentCalibration.nozzle_diameter == nozzle_dia,
                FilamentCalibration.nozzle_volume_type == nozzle_vol_type,
                FilamentCalibration.extruder_id == extruder_id,
                FilamentCalibration.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


_NOZZLE_PREFIX_TO_VOL_TYPE = {
    "HS": "standard",
    "HH": "high_flow",
    "HU": "tpu_high_flow",
    "HY": "hybrid",
}


def parse_nozzle_vol_type(nozzle_id: str | None) -> str:
    """Bambu nozzle IDs encode the volume class in their first two chars
    (``HS00-0.4`` = standard, ``HH00-0.4`` = high_flow, …). Unknown → standard."""
    if not nozzle_id:
        return "standard"
    prefix = nozzle_id[:2] if len(nozzle_id) >= 2 else ""
    return _NOZZLE_PREFIX_TO_VOL_TYPE.get(prefix, "standard")


def derive_effective_filament_id(*, spool=None, slot_tray_info_idx: str | None = None) -> str | None:
    """Pick the filament_id used for combo lookup.

    Precedence: spool's RFID-tagged ``bambu_filament_id`` → derived from
    ``slicer_filament`` → the slot's reported ``tray_info_idx``.
    """
    if spool is not None:
        bambu_id = getattr(spool, "bambu_filament_id", None)
        if bambu_id:
            return bambu_id
        slicer = getattr(spool, "slicer_filament", None)
        if slicer:
            from backend.app.utils.filament_ids import normalize_slicer_filament

            tray_info_idx, _setting_id = normalize_slicer_filament(slicer)
            if tray_info_idx:
                return tray_info_idx
    return slot_tray_info_idx or None


async def apply_active_calibration_to_slot(
    *,
    db: AsyncSession,
    printer_id: int,
    ams_id: int,
    slot_id: int,
    filament_id: str,
    nozzle_diameter: float,
    nozzle_volume_type: str = "standard",
    extruder_id: int = 0,
    spool_id: int | None = None,
) -> tuple[bool, FilamentCalibration | None]:
    """Resolve the right calibration for a slot and fire ``extrusion_cali_sel``.

    Resolution chain (each step falls through to the next when no match):
      1. Explicit ``spool_k_profile`` link when ``spool_id`` given.
      2. Active ``filament_calibration`` row by combo
         (``printer_id``, ``filament_id``, ``nozzle``, ``vol_type``, ``extruder``).

    With a cache row in hand:
      A. Pull stable identity (``name`` + ``pa_k_value`` + ``filament_id``).
      B. Re-match against ``client.state.kprofiles`` to find the LIVE
         ``cali_idx`` — the printer may have reordered since the cache row was
         written. Stored ``cali_idx`` is a hint only.
      C. Fire ``extrusion_cali_sel`` with the live ``cali_idx``.

    Returns ``(fired, cache_row)``. ``fired=True`` iff MQTT was published.
    Stale cache (live list lacks the profile) returns ``(False, cache_row)``
    so the caller can decide whether to fall back.
    """
    if not filament_id:
        return False, None

    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        return False, None

    cache_row: FilamentCalibration | None = None

    if spool_id is not None:
        from sqlalchemy.orm import selectinload

        from backend.app.models.spool_k_profile import SpoolKProfile

        link_rows = (
            (
                await db.execute(
                    select(SpoolKProfile)
                    .options(selectinload(SpoolKProfile.filament_calibration))
                    .where(
                        SpoolKProfile.spool_id == spool_id,
                        SpoolKProfile.printer_id == printer_id,
                        SpoolKProfile.extruder == extruder_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for ln in link_rows:
            fc = ln.filament_calibration
            if fc and abs(fc.nozzle_diameter - nozzle_diameter) < 0.05:
                cache_row = fc
                break

    if cache_row is None:
        cache_row = await resolve_active_calibration(
            db=db,
            printer_id=printer_id,
            filament_id=filament_id,
            nozzle_dia=nozzle_diameter,
            nozzle_vol_type=nozzle_volume_type,
            extruder_id=extruder_id,
        )
    if cache_row is None:
        return False, None

    target_k = cache_row.pa_k_value if cache_row.pa_k_value is not None else cache_row.flow_ratio
    if target_k is None or not cache_row.name:
        return False, cache_row

    live_match = None
    for kp in client.state.kprofiles or []:
        try:
            kp_k = float(kp.k_value)
        except (TypeError, ValueError):
            continue
        if kp.name == cache_row.name and abs(kp_k - float(target_k)) < 1e-6 and kp.filament_id == cache_row.filament_id:
            live_match = kp
            break
    if live_match is None:
        return False, cache_row

    try:
        client.extrusion_cali_sel(
            ams_id=ams_id,
            tray_id=slot_id,
            cali_idx=int(live_match.slot_id),
            filament_id=cache_row.filament_id,
            nozzle_diameter=str(nozzle_diameter),
        )
        return True, cache_row
    except Exception as e:
        logger.warning(
            "apply_active_calibration_to_slot failed printer=%s ams=%s slot=%s: %s",
            printer_id,
            ams_id,
            slot_id,
            e,
        )
        return False, cache_row


async def _first_admin_user_id(db: AsyncSession) -> int | None:
    """Return the id of the lowest-id admin.

    Matches the ``User.is_admin`` rule: either the legacy ``role='admin'``
    flag or membership in the ``Administrators`` group. Used as the
    placeholder ``calibrated_by_user_id`` on rows synced from the printer's
    own K-profile list — those entries weren't created by any specific
    BamDude session, so we stamp the canonical admin. Returns ``None`` if
    no such user exists, in which case the column stays NULL — non-fatal.
    """
    from sqlalchemy import or_

    from backend.app.models.group import Group
    from backend.app.models.user import User

    stmt = (
        select(User.id)
        .outerjoin(User.groups)
        .where(or_(User.role == "admin", Group.name == "Administrators"))
        .order_by(User.id.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar()


async def sync_printer_kprofiles_to_cache(
    *,
    db: AsyncSession,
    printer_id: int,
) -> int:
    """Mirror the printer's live K-profile list into our cache.

    Idempotent. For each entry in ``client.state.kprofiles``, find or create a
    ``filament_calibration`` row keyed by stable identity. Refreshes the
    cached ``cali_idx`` on existing rows (printer reorders happen).

    New rows ship as ``is_active=False`` so user-managed activation stays
    explicit (matches the m064 backfill choice). ``calibrated_by_user_id``
    is stamped with the first admin's id as a placeholder for "this row
    came from the printer, not from a wizard session".

    Returns the count of rows touched (created or refreshed).
    """
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        return 0
    live = client.state.kprofiles or []
    if not live:
        return 0

    default_user_id = await _first_admin_user_id(db)

    # State.nozzles is decoded per BS — pull flow class so we can fill in
    # nozzle_id for printers that don't ship a per-profile value (P1S, A1 mini).
    state_nozzles = getattr(client.state, "nozzles", []) or []

    touched = 0
    for kp in live:
        try:
            kp_k = float(kp.k_value)
        except (TypeError, ValueError):
            continue
        try:
            kp_nozzle_dia = float(kp.nozzle_diameter or 0.4)
        except (TypeError, ValueError):
            kp_nozzle_dia = 0.4
        extruder_id = int(getattr(kp, "extruder_id", 0) or 0)
        kp_nozzle_id = getattr(kp, "nozzle_id", None) or getattr(kp, "nozzle_type", None)
        if not kp_nozzle_id and 0 <= extruder_id < len(state_nozzles):
            flow = getattr(state_nozzles[extruder_id], "nozzle_flow", "") or "standard"
            try:
                kp_nozzle_id = generate_nozzle_id(NozzleVolumeType(flow), kp_nozzle_dia)
            except ValueError:
                kp_nozzle_id = None
        vol_type = parse_nozzle_vol_type(kp_nozzle_id)
        kp_filament_id = getattr(kp, "filament_id", "") or ""
        kp_name = getattr(kp, "name", "") or f"{kp_filament_id} K={kp_k:.4f}"
        kp_setting_id = getattr(kp, "setting_id", None)
        try:
            kp_slot_id = int(getattr(kp, "slot_id", -1))
        except (TypeError, ValueError):
            kp_slot_id = None

        existing = (
            await db.execute(
                select(FilamentCalibration).where(
                    FilamentCalibration.printer_id == printer_id,
                    FilamentCalibration.filament_id == kp_filament_id,
                    FilamentCalibration.nozzle_diameter == kp_nozzle_dia,
                    FilamentCalibration.nozzle_volume_type == vol_type,
                    FilamentCalibration.extruder_id == extruder_id,
                    FilamentCalibration.name == kp_name,
                    FilamentCalibration.pa_k_value == kp_k,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            db.add(
                FilamentCalibration(
                    printer_id=printer_id,
                    filament_id=kp_filament_id,
                    filament_setting_id=kp_setting_id,
                    nozzle_diameter=kp_nozzle_dia,
                    nozzle_volume_type=vol_type,
                    extruder_id=extruder_id,
                    pa_k_value=kp_k,
                    cali_mode="pa_line",
                    source="printer_sync",
                    is_active=False,
                    cali_idx=kp_slot_id,
                    name=kp_name,
                    nozzle_id=kp_nozzle_id,
                    calibrated_by_user_id=default_user_id,
                )
            )
            touched += 1
        else:
            row_touched = False
            if existing.cali_idx != kp_slot_id:
                existing.cali_idx = kp_slot_id
                row_touched = True
            # Backfill nozzle_id on existing cache rows that pre-date the
            # column (or were created before save_result started writing it).
            if not existing.nozzle_id and kp_nozzle_id:
                existing.nozzle_id = kp_nozzle_id
                row_touched = True
            # Mirror filament_setting_id from the printer — the printer is the
            # source of truth, so any drift (manual DB edit, stale cloud-preset
            # id captured before re-tag) reconciles back to the live value.
            if kp_setting_id and existing.filament_setting_id != kp_setting_id:
                existing.filament_setting_id = kp_setting_id
                row_touched = True
            if row_touched:
                touched += 1
    if touched:
        await db.commit()
    return touched
