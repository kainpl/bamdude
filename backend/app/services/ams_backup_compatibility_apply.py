"""Walk a printer's assigned slots through the projection — for the overlay rebuild and for bulk apply.

ONE walker (``iter_slot_projections``) so that what the startup rebuild
believes and what the operator sees in the bulk preview are the same answer.
No MQTT here except inside ``bulk_apply`` with ``dry_run=False``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import ams_advertised_overlay as overlay
from backend.app.services.ams_backup_compatibility import (
    REASON_SLOT_EMPTY,
    BackupCompatibilityPolicy,
    SlotProjection,
    kprofile_allowed,
    live_tray_for,
    project_slot_assignment,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.slot_assignment import SlotAssignmentPlan, build_slot_assignment
from backend.app.services.slot_assignment_publish import publish_slot_plan

logger = logging.getLogger(__name__)


@dataclass
class SlotCandidate:
    ams_id: int
    tray_id: int
    source: str
    spool_label: str
    projection: SlotProjection
    live_tray: dict | None
    kprofile_filament_id: str | None


def _nozzle(state) -> str:
    nozzles = getattr(state, "nozzles", None) or []
    nd = getattr(nozzles[0], "nozzle_diameter", None) if nozzles else None
    return nd or "0.4"


def _slot_loaded(live_tray: dict | None) -> bool:
    from backend.app.api.routes.inventory import tray_holds_filament  # noqa: PLC0415 — the route owns the reading

    return bool(live_tray) and tray_holds_filament(live_tray)


async def iter_slot_projections(db, printer) -> list[SlotCandidate]:
    policy = BackupCompatibilityPolicy.from_printer(printer)
    state = printer_manager.get_status(printer.id)
    nozzle = _nozzle(state)
    supports = bool(getattr(state, "support_user_preset", False))
    common = {"printer_model": printer.model, "nozzle_diameter": nozzle, "supports_user_preset": supports}
    out: list[SlotCandidate] = []

    rows = (
        (
            await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(SpoolAssignment.printer_id == printer.id)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        spool = row.spool
        if spool is None:
            continue
        try:
            actual = await build_slot_assignment(db, spool=spool, **common)
        except ValueError as exc:
            logger.info("bulk projection: skipping AMS%d-T%d: %s", row.ams_id, row.tray_id, exc)
            continue
        live = live_tray_for(state, row.ams_id, row.tray_id)
        projection = await project_slot_assignment(
            db,
            actual=actual,
            policy=policy,
            live_tray=live,
            spool_tag_uid=spool.tag_uid,
            spool_tray_uuid=spool.tray_uuid,
            ams_id=row.ams_id,
            material=spool.material,
            extra_colors=spool.extra_colors,
            **common,
        )
        label = " ".join(p for p in (f"#{spool.id}", spool.brand or "", spool.material) if p)
        out.append(SlotCandidate(row.ams_id, row.tray_id, "internal", label, projection, live, actual.tray_info_idx))

    sm_rows = (
        (await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer.id)))
        .scalars()
        .all()
    )
    if sm_rows:
        from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool  # noqa: PLC0415
        from backend.app.services.spoolman import get_spoolman_client  # noqa: PLC0415

        client = await get_spoolman_client()
        for row in sm_rows:
            if client is None:
                break
            try:
                mapped = _map_spoolman_spool(await client.get_spool(row.spoolman_spool_id))
            except Exception as exc:  # noqa: BLE001 — Spoolman down must not break the walk
                logger.info("bulk projection: Spoolman spool %s unreadable: %s", row.spoolman_spool_id, exc)
                continue
            tray_type = mapped.get("material") or ""
            color = (mapped.get("rgba") or "808080FF").upper()
            color = color + "FF" if len(color) == 6 else color
            try:
                # Family = generic of the material. The assign route may have used
                # the K-linked branded family instead; only the VARIANT can differ,
                # and only until the next assignment rewrites the entry exactly.
                actual = await build_slot_assignment(
                    db,
                    family_id=None,
                    material_override=tray_type,
                    color_rgba=color,
                    temp_overrides=(mapped.get("nozzle_temp_min"), None),
                    **common,
                )
            except ValueError:
                continue
            live = live_tray_for(state, row.ams_id, row.tray_id)
            projection = await project_slot_assignment(
                db,
                actual=actual,
                policy=policy,
                live_tray=live,
                spool_tag_uid=mapped.get("tag_uid"),
                spool_tray_uuid=mapped.get("tray_uuid"),
                ams_id=row.ams_id,
                material=tray_type,
                extra_colors=None,
                **common,
            )
            label = " ".join(
                p for p in (f"Spoolman #{row.spoolman_spool_id}", mapped.get("brand") or "", tray_type) if p
            )
            out.append(
                SlotCandidate(row.ams_id, row.tray_id, "spoolman", label, projection, live, actual.tray_info_idx)
            )

    out.sort(key=lambda c: (c.ams_id, c.tray_id))
    return out


async def refresh_overlay(db, printer) -> None:
    """Rebuild this printer's overlay from the registries + policy. Startup only — a live entry is exact, this one is derived."""
    try:
        candidates = await iter_slot_projections(db, printer)
    except Exception:  # noqa: BLE001 — never fail startup over an overlay
        logger.exception("overlay refresh failed for printer %s", printer.id)
        return
    overlay.replace_printer(
        printer.id,
        {
            (c.ams_id, c.tray_id): overlay.entry_from(c.projection, c.source)
            for c in candidates
            if c.projection.projected
        },
    )


def _plan_dict(plan: SlotAssignmentPlan) -> dict:
    return {
        "tray_info_idx": plan.tray_info_idx,
        "tray_type": plan.tray_type,
        "tray_color": plan.tray_color,
        "cols": list(plan.cols),
        "setting_id": plan.setting_id,
    }


def _slot_label(ams_id: int, tray_id: int) -> str:
    from backend.app.services.spool_assignment_notifications import _slot_label_from_global_tray  # noqa: PLC0415

    return _slot_label_from_global_tray(ams_id if ams_id >= 128 else ams_id * 4 + tray_id)


async def bulk_apply(db, printer, client, *, dry_run: bool) -> dict:
    candidates = await iter_slot_projections(db, printer)
    nozzle = _nozzle(printer_manager.get_status(printer.id))
    remembered = overlay.entries_for(printer.id)
    rows: list[dict] = []
    for c in candidates:
        reasons = list(c.projection.reasons)
        loaded = _slot_loaded(c.live_tray)
        if not loaded:
            reasons.append(REASON_SLOT_EMPTY)
        # "revert": the policy no longer projects this slot but we once told the
        # printer something else — re-advertise the ACTUAL plan and forget.
        revert = not c.projection.projected and (c.ams_id, c.tray_id) in remembered
        if not loaded:
            action = "skip"
        elif c.projection.projected:
            action = "apply"
        elif revert:
            action = "revert"
        else:
            action = "skip"
        rows.append(
            {
                "ams_id": c.ams_id,
                "tray_id": c.tray_id,
                "slot": _slot_label(c.ams_id, c.tray_id),
                "source": c.source,
                "spool": c.spool_label,
                "actual": _plan_dict(c.projection.actual),
                "advertised": _plan_dict(c.projection.advertised),
                "action": action,
                "reasons": reasons,
                "published": None,
                "kprofile": None,
                "_candidate": c,
            }
        )
    would_apply = sum(1 for r in rows if r["action"] in ("apply", "revert"))
    if dry_run:
        for r in rows:
            r.pop("_candidate")
        return {
            "dry_run": True,
            "rows": rows,
            "applied": 0,
            "skipped": len(rows) - would_apply,
            "would_apply": would_apply,
        }

    applied = 0
    for r in rows:
        c: SlotCandidate = r.pop("_candidate")
        if r["action"] == "skip":
            continue
        plan = c.projection.advertised if r["action"] == "apply" else c.projection.actual
        r["published"] = publish_slot_plan(client, ams_id=c.ams_id, tray_id=c.tray_id, plan=plan)
        if not r["published"]:
            continue
        applied += 1
        overlay.remember(printer.id, c.ams_id, c.tray_id, c.projection, c.source)  # forgets on a revert (not projected)
        live_cali = (c.live_tray or {}).get("cali_idx")
        if isinstance(live_cali, int) and live_cali >= 0:
            if kprofile_allowed(c.projection) and c.kprofile_filament_id:
                # Re-sending ams_filament_setting may reset the slot's K; put the
                # live one back — the Spoolman assign route does the same.
                client.extrusion_cali_sel(
                    ams_id=c.ams_id,
                    tray_id=c.tray_id,
                    cali_idx=live_cali,
                    filament_id=c.kprofile_filament_id,
                    nozzle_diameter=nozzle,
                )
                r["kprofile"] = "kept"
            else:
                r["kprofile"] = "skipped"
    return {"dry_run": False, "rows": rows, "applied": applied, "skipped": len(rows) - applied}
