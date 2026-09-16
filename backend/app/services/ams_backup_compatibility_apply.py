"""Walk a printer's assigned slots through the projection — for the overlay rebuild and for bulk apply.

ONE walker (``iter_slot_projections``) so that what the deferred rebuild
believes and what the operator sees in the bulk preview are the same answer.
No MQTT here except inside ``bulk_apply`` with ``dry_run=False``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

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


class RebuildOutcome(Enum):
    """Why a rebuild stopped — three answers, not two.

    ``COMPLETE`` replaced the printer's map. ``DEFERRED`` means the Spoolman
    half could not be read and the map was left ALONE. ``FAILED`` means the
    walk threw: also nothing written, but it is a bug to be retried, not a
    startup race to be waited out, and the two must not share a code path —
    marking a printer rebuilt because the walk crashed is how an overlay stays
    empty for the life of the process.
    """

    COMPLETE = "complete"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass
class SlotCandidate:
    ams_id: int
    tray_id: int
    source: str
    spool_label: str
    projection: SlotProjection
    live_tray: dict | None
    kprofile_filament_id: str | None
    # What we must ALREADY have advertised on this slot, reconstructed when the
    # current policy no longer projects it (see ``_recover_advertised``).
    recovered: overlay.OverlayEntry | None = None


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
    # The nozzle the walk projected against, carried out so a caller that needs
    # it (the K re-push in ``bulk_apply``) reads the state ONCE — asking the
    # manager again could answer a different device than the one these
    # candidates were built for.
    nozzle_diameter: str = "0.4"


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


async def _spoolman_mode(db) -> bool | None:
    """Whether this install's inventory IS Spoolman (spec §4.3: the walk covers
    the slots of the CURRENT mode) — or ``None`` when we could not find out.

    Read exactly as ``print_scheduler._is_spoolman_mode`` reads it. In internal
    mode the Spoolman client will never come up, so a walk that waited for it
    would defer forever in a hot MQTT callback; leftover
    ``spoolman_slot_assignments`` rows from a previous mode are not this
    install's slots.

    ⚠️ A read that FAILED is the third answer, not the "no" one: a transient
    settings error would otherwise pass for internal mode, the walk would drop
    the whole Spoolman half and still report COMPLETE, and the rebuild would
    replace a correct overlay with a halved one — once per process, never asked
    again.
    """
    try:
        from backend.app.api.routes.settings import get_setting  # noqa: PLC0415

        value = await get_setting(db, "spoolman_enabled")
        return bool(value) and str(value).lower() == "true"
    except Exception:  # noqa: BLE001 — unknown, and unknown is not "no"
        logger.info("bulk projection: could not read spoolman_enabled; deferring the Spoolman half", exc_info=True)
        return None


async def _recover_advertised(
    db, *, actual, policy: BackupCompatibilityPolicy, live_tray: dict | None, source: str, **project_kwargs
) -> overlay.OverlayEntry | None:
    """What we advertised on a slot the CURRENT policy no longer projects.

    A restart with the switches off would otherwise strand every masked slot:
    the walk projects ``policy_off`` for all of them, the overlay is emptied,
    routing believes the masked live values and the bulk button never offers a
    ``revert`` — the section hides, because it renders off the entries. So each
    candidate policy we could once have been running under is re-projected
    (colour with the STORED canonical colour even though the switch is off,
    generic, both) and the one whose ADVERTISED plan the live tray still equals
    is the one we must have sent.

    A live tray matching none of them was never ours — no entry, and the
    existing auto-unlink semantics decide its fate as before. A tray matching
    one that somebody set by hand on the printer screen is indistinguishable
    from ours and is adopted; that is the accepted cost of not stranding the
    real case, and it can only ever report the slot's own assigned spool.
    """
    stored = policy.canonical_color_rgba
    candidates = (
        BackupCompatibilityPolicy(normalize_color=True, canonical_color_rgba=stored),
        BackupCompatibilityPolicy(generic_base_material=True),
        BackupCompatibilityPolicy(normalize_color=True, canonical_color_rgba=stored, generic_base_material=True),
    )
    for candidate in candidates:
        projection = await project_slot_assignment(
            db, actual=actual, policy=candidate, live_tray=live_tray, **project_kwargs
        )
        if not projection.projected:
            continue
        entry = overlay.entry_from(projection, source)
        if overlay.matches_live(entry, live_tray):
            return entry
    return None


def _slot_loaded(live_tray: dict | None) -> bool:
    from backend.app.api.routes.inventory import tray_holds_filament  # noqa: PLC0415 — the route owns the reading

    return bool(live_tray) and tray_holds_filament(live_tray)


async def iter_slot_projections(db, printer) -> SlotWalk:
    policy = BackupCompatibilityPolicy.from_printer(printer)
    state = printer_manager.get_status(printer.id)
    nozzle = _nozzle(state)
    supports = bool(getattr(state, "support_user_preset", False))
    common = {"printer_model": printer.model, "nozzle_diameter": nozzle, "supports_user_preset": supports}
    walk = SlotWalk(nozzle_diameter=nozzle)
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
        project_kwargs = {
            "spool_tag_uid": spool.tag_uid,
            "spool_tray_uuid": spool.tray_uuid,
            "ams_id": row.ams_id,
            "material": spool.material,
            "extra_colors": spool.extra_colors,
            **common,
        }
        projection = await project_slot_assignment(db, actual=actual, policy=policy, live_tray=live, **project_kwargs)
        recovered = (
            None
            if projection.projected
            else await _recover_advertised(
                db, actual=actual, policy=policy, live_tray=live, source="internal", **project_kwargs
            )
        )
        label = " ".join(p for p in (f"#{spool.id}", spool.brand or "", spool.material) if p)
        out.append(
            SlotCandidate(row.ams_id, row.tray_id, "internal", label, projection, live, actual.tray_info_idx, recovered)
        )

    sm_rows = (
        (await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer.id)))
        .scalars()
        .all()
    )
    if sm_rows:
        mode = await _spoolman_mode(db)
        if mode is None:
            # We do not know which inventory this is. Neither reading the rows
            # nor dropping them is an answer, so the walk says so and keeps its
            # hands off the map.
            walk.spoolman_deferred = True
            sm_rows = []
        elif not mode:
            # Not this install's inventory — the rows are leftovers of a mode
            # that was switched off, and nothing will ever read them. Absent,
            # not deferred: deferring would retry on every AMS push forever.
            sm_rows = []
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
                # Unreachable is the same answer as "the client is not up yet":
                # the slot HAS a spool, we just could not read it. Counting it
                # as absent would make an incomplete walk look complete, and
                # the rebuild would replace a correct overlay with a halved one.
                logger.info("bulk projection: Spoolman spool %s unreadable: %s", row.spoolman_spool_id, exc)
                walk.spoolman_deferred = True
                continue
            tray_type = mapped.get("material") or ""
            # Already 8 chars: ``_map_spoolman_spool`` validates the hex and
            # appends the alpha itself, so there is no 6-char form to pad here.
            color = (mapped.get("rgba") or "808080FF").upper()
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
            project_kwargs = {
                "spool_tag_uid": mapped.get("tag_uid"),
                "spool_tray_uuid": mapped.get("tray_uuid"),
                "ams_id": row.ams_id,
                "material": tray_type,
                "extra_colors": None,
                **common,
            }
            projection = await project_slot_assignment(
                db, actual=actual, policy=policy, live_tray=live, **project_kwargs
            )
            recovered = (
                None
                if projection.projected
                else await _recover_advertised(
                    db, actual=actual, policy=policy, live_tray=live, source="spoolman", **project_kwargs
                )
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
                    recovered,
                )
            )

    out.sort(key=lambda c: (c.ams_id, c.tray_id))
    return walk


def _entries_for_walk(walk: SlotWalk) -> dict[tuple[int, int], overlay.OverlayEntry]:
    entries: dict[tuple[int, int], overlay.OverlayEntry] = {}
    for c in walk.candidates:
        # ``slot_key``, not a hand-built tuple: the store answers on that key
        # alone, and an HT slot is the one place the two can differ.
        if c.projection.projected:
            entries[overlay.slot_key(c.ams_id, c.tray_id)] = overlay.entry_from(c.projection, c.source)
        elif c.recovered is not None:
            entries[overlay.slot_key(c.ams_id, c.tray_id)] = c.recovered
    return entries


async def refresh_overlay(db, printer) -> RebuildOutcome:
    """Rebuild this printer's overlay from the registries + policy.

    A live entry is exact; this one is derived, so it is a recovery after a
    restart, not the normal way entries appear — which is why ONLY a complete
    walk replaces the map. An incomplete one (Spoolman unreadable) and a walk
    that threw both leave whatever the assignment routes have written since
    exactly where it is; replacing on a half answer would delete the exact
    entries of every Spoolman slot and call the result a rebuild.
    """
    try:
        walk = await iter_slot_projections(db, printer)
    except Exception:  # noqa: BLE001 — never fail a callback over an overlay
        logger.exception("overlay refresh failed for printer %s", printer.id)
        return RebuildOutcome.FAILED
    if walk.spoolman_deferred:
        return RebuildOutcome.DEFERRED
    overlay.replace_printer(printer.id, _entries_for_walk(walk))
    return RebuildOutcome.COMPLETE


# Which printers this process has already rebuilt. Once per process, because
# the live entries written by the assignment routes afterwards are exact and a
# second derived pass could only overwrite them with a guess.
_rebuilt: set[int] = set()
# How many times each printer's rebuild came back incomplete. This runs inside
# ``on_ams_change``, which fires several times a minute per printer: a Spoolman
# that never comes back must not buy an unbounded retry on a hot callback.
_deferred_attempts: dict[int, int] = {}
MAX_DEFERRED_ATTEMPTS = 5


def reset_rebuilt() -> None:
    """Tests only — the set is process-global and outlives a test."""
    _rebuilt.clear()
    _deferred_attempts.clear()


def forget_printer_rebuild(printer_id: int) -> None:
    """A deleted printer's id is free again; nothing about it may be remembered."""
    _rebuilt.discard(printer_id)
    _deferred_attempts.pop(printer_id, None)


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
        outcome = await refresh_overlay(db, printer)
    if outcome is RebuildOutcome.FAILED:
        # Nothing was written and nothing is known — ask again on the next push
        # rather than spend this printer's one rebuild on an exception.
        return
    if outcome is RebuildOutcome.DEFERRED:
        attempts = _deferred_attempts[printer_id] = _deferred_attempts.get(printer_id, 0) + 1
        if attempts < MAX_DEFERRED_ATTEMPTS:
            logger.info("overlay rebuild for printer %s deferred: Spoolman not readable", printer_id)
            return
        logger.warning(
            "overlay rebuild for printer %s gave up after %d deferred attempts: the Spoolman half stayed unreadable, "
            "so the map was never replaced — every slot of this printer keeps whatever the overlay already holds "
            "(nothing at all after a restart) until it is re-assigned or the bulk apply is run",
            printer_id,
            attempts,
        )
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
    walk = await iter_slot_projections(db, printer)
    candidates = walk.candidates
    nozzle = walk.nozzle_diameter  # the state the walk read, not a second one
    remembered = overlay.entries_for(printer.id)
    rows: list[dict] = []
    for c in candidates:
        reasons = list(c.projection.reasons)
        loaded = _slot_loaded(c.live_tray)
        if not loaded:
            reasons.append(REASON_SLOT_EMPTY)
        # "revert": the policy no longer projects this slot but we once told the
        # printer something else — re-advertise the ACTUAL plan and forget.
        # ``recovered`` answers the same question for a slot whose entry this
        # process never held: after a restart with the switches already off the
        # store is empty, and only the reconstruction knows the tray is masked.
        revert = not c.projection.projected and (
            overlay.slot_key(c.ams_id, c.tray_id) in remembered or c.recovered is not None
        )
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
            # The list is HALVED, not empty, when Spoolman could not be read —
            # say so, or the operator reads "3 slots" as the whole farm.
            "spoolman_unavailable": walk.spoolman_deferred,
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
    return {
        "dry_run": False,
        "rows": rows,
        "applied": applied,
        "skipped": len(rows) - applied,
        "spoolman_unavailable": walk.spoolman_deferred,
    }
