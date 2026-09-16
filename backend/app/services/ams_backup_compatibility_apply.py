"""Walk a printer's assigned slots through the projection — for the overlay rebuild and for bulk apply.

ONE walker (``iter_slot_projections``) so that what the deferred rebuild
believes and what the operator sees in the bulk preview are the same answer.
No MQTT here except inside ``bulk_apply`` with ``dry_run=False``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.models.printer import Printer
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
from backend.app.services.spoolman_kprofile_link import resolve_spoolman_slot_kprofile

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


@dataclass
class SlotWalk:
    """One pass over both registries.

    ``spoolman_deferred`` is the difference between "this printer has no
    Spoolman slots" and "we could not read them": the Spoolman client is built
    late in startup, and a walk that ran before it silently drops the whole
    Spoolman registry. The caller decides whether that answer is good enough —
    a preview says what it found, the rebuild retries.
    """

    candidates: list[SlotCandidate] = field(default_factory=list)
    spoolman_deferred: bool = False


def _nozzle(state) -> str:
    nozzles = getattr(state, "nozzles", None) or []
    nd = getattr(nozzles[0], "nozzle_diameter", None) if nozzles else None
    return nd or "0.4"


def _slot_extruder(state, ams_id: int, tray_id: int) -> int | None:
    """Which extruder feeds this slot — exactly what the two Spoolman assign routes derive.

    None on every printer that reports no map (single-extruder): the K link
    still applies, it simply cannot be preferred over another one.
    """
    extruder_map = getattr(state, "ams_extruder_map", None)
    if not extruder_map:
        return None
    if ams_id == 255:
        # External: ext-L (tray 0) → extruder 1, ext-R (tray 1) → extruder 0.
        return 1 - tray_id
    return extruder_map.get(str(ams_id))


def _slot_loaded(live_tray: dict | None) -> bool:
    from backend.app.api.routes.inventory import tray_holds_filament  # noqa: PLC0415 — the route owns the reading

    return bool(live_tray) and tray_holds_filament(live_tray)


async def iter_slot_projections(db, printer) -> SlotWalk:
    policy = BackupCompatibilityPolicy.from_printer(printer)
    state = printer_manager.get_status(printer.id)
    nozzle = _nozzle(state)
    supports = bool(getattr(state, "support_user_preset", False))
    common = {"printer_model": printer.model, "nozzle_diameter": nozzle, "supports_user_preset": supports}
    walk = SlotWalk()
    out = walk.candidates

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
        if client is None:
            # Unreadable, not empty — the caller must not take a silently
            # halved farm for the whole answer.
            walk.spoolman_deferred = True
            sm_rows = []
        try:
            nozzle_float = float(nozzle)
        except (TypeError, ValueError):
            nozzle_float = 0.4
        for row in sm_rows:
            try:
                mapped = _map_spoolman_spool(await client.get_spool(row.spoolman_spool_id))
            except Exception as exc:  # noqa: BLE001 — Spoolman down must not break the walk
                logger.info("bulk projection: Spoolman spool %s unreadable: %s", row.spoolman_spool_id, exc)
                continue
            tray_type = mapped.get("material") or ""
            color = (mapped.get("rgba") or "808080FF").upper()
            color = color + "FF" if len(color) == 6 else color
            # The family is the BRANDED one of the slot's linked calibration —
            # the same resolver both Spoolman assign routes use, so a revert
            # republishes the plan the slot actually had and the K re-push
            # carries the id those routes key K off.
            linked = await resolve_spoolman_slot_kprofile(
                db,
                printer_id=printer.id,
                spoolman_spool_id=row.spoolman_spool_id,
                nozzle_diameter=nozzle_float,
                slot_extruder=_slot_extruder(state, row.ams_id, row.tray_id),
            )
            try:
                actual = await build_slot_assignment(
                    db,
                    family_id=linked.filament_id if linked else None,
                    material_override=tray_type,
                    color_rgba=color,
                    temp_overrides=(mapped.get("nozzle_temp_min"), None),
                    **common,
                )
            except ValueError as exc:
                logger.info("bulk projection: skipping Spoolman AMS%d-T%d: %s", row.ams_id, row.tray_id, exc)
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
                SlotCandidate(
                    row.ams_id,
                    row.tray_id,
                    "spoolman",
                    label,
                    projection,
                    live,
                    linked.filament_id if linked else actual.tray_info_idx,
                )
            )

    out.sort(key=lambda c: (c.ams_id, c.tray_id))
    return walk


async def refresh_overlay(db, printer) -> bool:
    """Rebuild this printer's overlay from the registries + policy.

    Returns whether the answer was INCOMPLETE because the Spoolman client was
    not up yet — the caller then leaves the printer unmarked and asks again on
    its next AMS push. A live entry is exact; this one is derived, so it is a
    recovery after a restart, not the normal way entries appear.
    """
    try:
        walk = await iter_slot_projections(db, printer)
    except Exception:  # noqa: BLE001 — never fail a callback over an overlay
        logger.exception("overlay refresh failed for printer %s", printer.id)
        return False
    overlay.replace_printer(
        printer.id,
        {
            (c.ams_id, c.tray_id): overlay.entry_from(c.projection, c.source)
            for c in walk.candidates
            if c.projection.projected
        },
    )
    return walk.spoolman_deferred


# Which printers this process has already rebuilt. Once per process, because
# the live entries written by the assignment routes afterwards are exact and a
# second derived pass could only overwrite them with a guess.
_rebuilt: set[int] = set()


def reset_rebuilt() -> None:
    """Tests only — the set is process-global and outlives a test."""
    _rebuilt.clear()


async def rebuild_once(printer_id: int) -> None:
    """Rebuild this printer's overlay the first time we see its AMS in this process.

    Deliberately NOT at startup: there the printer has no MQTT client yet, so
    ``get_status`` answers None and every slot would be projected against a
    made-up device — no user presets, a 0.4 nozzle — which degrades every
    ``P*`` family to its generic and leaves entries that can never match the
    printer's own echo, i.e. dormant forever. By the first ``on_ams_change``
    the whole pushall has been parsed and the Spoolman client is up.
    """
    if printer_id in _rebuilt:
        return
    # Looked up at call time on purpose: the test harness swaps this attribute
    # on the module, and a name bound at import would keep the real engine.
    from backend.app.core.database import async_session  # noqa: PLC0415

    async with async_session() as db:
        printer = (
            await db.execute(select(Printer).where(Printer.id == printer_id, Printer.archived.is_(False)))
        ).scalar_one_or_none()
        if printer is None:
            return
        deferred = await refresh_overlay(db, printer)
    if deferred:
        logger.info("overlay rebuild for printer %s deferred: Spoolman client not ready", printer_id)
        return
    _rebuilt.add(printer_id)


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
    candidates = (await iter_slot_projections(db, printer)).candidates
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
