"""Read-only dispatch preflight and a synchronous guard at the MQTT boundary."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace

from backend.app.models.queue_source import FORMAT_GCODE
from backend.app.services.filament_intake import (
    item_descriptor,
    item_source,
    read_item_requirements,
    resolve_source_path,
)
from backend.app.services.filament_policy import decode, queue_policy, source_scope
from backend.app.services.filament_requirements import probe_identity, revision_refutes
from backend.app.services.filament_routing import (
    RoutingDeferred,
    channel_nozzle_counts,
    channel_refusal,
    effective_slots,
    feed_preconditions,
    fingerprint,
    normalized_color,
    resolve_filament_routing,
    slot_nozzle,
    source_fits,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.source_io import SourceUnavailable
from backend.app.utils.printer_configs import requires_left_tpu_firmware_check
from backend.app.utils.tray_ids import is_external_id

#: The one deadline of ``settle_plan``: how long a prepared attempt waits for the
#: plan to hold again — a reconnected printer's first complete report, an empty
#: planned slot the operator is refilling (spec direct-print-silent-cancel §4.3,
#: dispatch-guard-follows-the-plan Д6; owner's В2, 2026-09-30).
FEED_SETTLE_TIMEOUT = 60.0
FEED_SETTLE_POLL = 1.0
#: The grace inside that deadline for a planned slot that is loaded but does not
#: fit yet — separately reported facts (the FTS confirmation, a nozzle diameter)
#: or BamDude's own slot writes catching up — before ``final_guard`` names it.
FEED_SETTLE_CONVERGE = 5.0


@dataclass(frozen=True)
class DispatchRoutingGuard:
    requirements: object
    policy: object
    plan: object
    exact_model: bool
    revision: str
    #: The MQTT session the plan was last confirmed on. The pre-start K-profile
    #: bind went to that session, and ``_on_connect`` drops whatever paho was
    #: still retrying — a print published on another session would start with
    #: no K selected (review 2026-09-30).
    generation: int
    #: The nozzle diameters the plan's channels print from, as the preflight saw
    #: them (``planned_nozzle_diameters``). A plate that names no diameter, or an
    #: H2C rack whose dock was re-fitted during the soak, would otherwise start
    #: on a hotend the plan was never made for (final review I1).
    nozzle_diameters: dict[int, tuple[float, ...]]
    #: ``requires_left_tpu_firmware_check(model)``, answered when the guard was
    #: built: its first call reads the mirrored printer config from disk, and
    #: ``validate`` runs under the MQTT routing lock, where nothing may. No
    #: default — a safety check must never be off because an argument was forgotten.
    left_tpu_check: bool
    #: None before the durable sending phase. An empty tuple still proves
    #: that no colour difference was recorded for this attempt.
    committed_color_changes: tuple[tuple[int, str, str], ...] | None = None

    def color_changes(self, snapshot=None) -> tuple[tuple[int, str, str], ...]:
        """Compare the file's colour to the physical colour of each mapped tray."""
        sources = None if snapshot is None else {source.id: source for source in snapshot.sources}
        changes = []
        for filament in self.requirements.used_filaments:
            slot_id = filament["slot_id"]
            source = self.plan.assignments.get(slot_id)
            if source is not None and sources is not None:
                source = sources.get(source.id)
            requested = normalized_color(filament.get("color"))
            loaded = normalized_color(source.color) if source is not None else None
            if requested and loaded and requested != loaded:
                changes.append((slot_id, f"#{requested}", f"#{loaded}"))
        return tuple(changes)

    def validate(self, snapshot, *, mapping, use_ams, plate_id):
        """Under the client's routing lock: no await and no file read before publish."""
        # Source I/O is checked by final_guard before this synchronous handoff.
        # Never stat a network mount while holding the MQTT telemetry lock.
        # ``plan_holds`` is pure arithmetic over a snapshot already in hand; the
        # model check (the only one that can read a config file) is skipped.
        if not snapshot.connected:
            raise RoutingDeferred("printer_offline")
        if snapshot.generation != self.generation:
            raise RoutingDeferred("printer_reconnected")
        holds, reason, _unknown = plan_holds(self, snapshot, with_model=False)
        if not holds:
            raise RoutingDeferred(reason or "feed_state_changed")
        if mapping != self.plan.mapping or use_ams != self.plan.use_ams or plate_id != self.plan.resolved_plate_id:
            raise RoutingDeferred("mapping_review_required")
        if self.committed_color_changes is not None and self.color_changes(snapshot) != self.committed_color_changes:
            raise RoutingDeferred("feed_state_changed")


def plan_holds(guard, snapshot, *, with_model: bool = True) -> tuple[bool, str | None, bool]:
    """Does the prepared plan still hold on this snapshot — asked of the plan's own slots.

    ``(holds, reason, unknown)``. The same rule the resolver chose the plan by
    (``filament_routing.feed_preconditions``, ``channel_refusal``,
    ``source_fits``), asked of the sources the plan CHOSE: a spool swapped in a
    slot the job does not use, a new tag on the same filament or a remain update
    is not a changed plan (owner, 2026-09-30; spec dispatch-guard-follows-the-
    plan Д4). Pure arithmetic over the snapshot. ``planned_source_empty`` comes
    with ``unknown=True``: a planned slot is empty now — an operator mid-swap —
    which ``settle_plan`` waits on. None of these refusals carries a revision,
    so none latches.
    """
    req, policy, plan = guard.requirements, guard.policy, guard.plan
    refusal = feed_preconditions(req, policy, snapshot, exact_model=guard.exact_model, with_model=with_model)
    if refusal is not None:
        return False, refusal.reason, refusal.status == "unknown"
    slots = effective_slots(req, policy)
    if slots is None:
        return False, "override_slot_not_used", False  # nothing the printer reports can change it
    now_fitted = planned_nozzle_diameters(req, policy, plan, snapshot)
    for nozzle, fitted in guard.nozzle_diameters.items():
        if not fitted:
            continue  # the preflight never knew it; the plate's own diameter check still stands
        if not now_fitted.get(nozzle):
            return False, "nozzle_state_unavailable", True
        if now_fitted[nozzle] != fitted:
            return False, "nozzle_mismatch", False
    present = {source.id: source for source in snapshot.sources}
    nozzle_counts = channel_nozzle_counts(slots)
    for slot in slots:
        planned = plan.assignments.get(slot["slot_id"])
        if planned is None:
            # A complete plan assigns every used channel; one that does not is
            # not a plan this guard may pass.
            return False, "mapping_review_required", False
        refusal = channel_refusal(req, slot, snapshot)
        if refusal is not None:
            return False, refusal.reason, refusal.status == "unknown"
        source = present.get(planned.id)
        if source is None:
            return False, "planned_source_empty", True
        fits, why, unsure = source_fits(
            slot,
            source,
            policy,
            snapshot,
            nozzle=slot_nozzle(slot),
            nozzle_counts=nozzle_counts,
            allowed=None,
            pin=policy.physical_pins.get(slot["slot_id"]),
            left_tpu_check=guard.left_tpu_check,
        )
        if not fits:
            return False, why or "material_mismatch", unsure
    return True, None, False


def planned_nozzle_diameters(req, policy, plan, snapshot) -> dict[int, tuple[float, ...]]:
    """The fitted diameters of the nozzles the plan's channels print from.

    Pure arithmetic over the snapshot (``validate`` asks it under the lock).
    Only the plan's nozzles: a hotend swapped on a nozzle the job does not use
    is not a changed plan. On an H2C this is the whole rack's diameter set for
    the rack carriage — a per-dock swap that leaves the set unchanged is not
    visible here.
    """
    if getattr(snapshot, "direct_rack", False) is True:
        # A direct rack: every channel is on extruder 0, and the hotend that prints
        # a channel is the one its planned tray leads to — keyed by that hotend's
        # dock ``addr``, so a swap on a planned PrintTool is a changed plan.
        out: dict[int, tuple[float, ...]] = {}
        for source in plan.assignments.values():
            hotend = snapshot.rack.hotend(source.rack_addr)
            if hotend is not None:
                out[hotend.addr] = (hotend.diameter,) if hotend.diameter else ()
        return dict(sorted(out.items()))
    nozzles = {
        slot_nozzle(slot) for slot in (effective_slots(req, policy) or []) if slot["slot_id"] in plan.assignments
    }
    return {nozzle: tuple(snapshot.nozzle_diameters.get(nozzle, ())) for nozzle in sorted(nozzles)}


def feed_signature(policy, snapshot) -> tuple[int, str]:
    """Whether the feed has moved, asked under one job's own policy — the
    auto-queue's placement check and the latch key; the dispatch guard asks
    ``plan_holds`` instead.

    ``PrinterFeedSnapshot.revision`` hashes a tray's ``tray_info_idx`` with
    everything else, so re-tagging a spool in the AMS moves the marker of every
    printer that holds it. That is the right answer for a job that asked for one
    exact profile and the wrong one for a job that said «any ABS will do»: with
    «allow base material match» on, no profile id takes part in routing, so a job
    prepared minutes ago was stopped — or kept stopped — by a fact its own plan
    had already been told to ignore.

    With the option OFF this IS the snapshot's own marker, unchanged, which is
    also why a block recorded by an older build still matches for such a job.
    With it ON the same facts are re-hashed without ``variant``. Everything
    physical survives verbatim: each source's tag (``tray_uuid``/``tag_uid``),
    material, colour, nozzle binding, feed kind and slot id, plus the connection
    generation and the shape of the feed itself. A swapped spool, a re-coloured
    one, a lost AMS or a reconnect all still move this. With it ON,
    ``declared_variant`` is left out beside ``variant``: re-advertising a Generic
    family moves no filament either.

    The one fact of ``snapshot_from_state``'s own payload that cannot travel here
    is which sources an advertised-profile overlay masked: it is folded into the
    revision but not exposed on the snapshot. An overlay whose actual values
    equal the live ones is therefore invisible to this signature — and to the
    resolver too, which sees identical ``FeedSource`` rows either way.

    ⚠️ In the other direction, ``backup_enabled`` makes the ON signature STRICTER
    than the raw marker on that one axis: the revision does not hash it, this
    does. So the first status push that fills it in between preflight and publish
    defers an ON job once, on a fact that changed nothing about the trays. The
    next preflight re-reads it and the job goes — accepted rather than papered
    over, because a feed whose backup state we have only just learned is a feed
    we were routing against half-known.
    """
    if not getattr(policy, "allow_base_material_match", False):
        return snapshot.marker
    return (
        snapshot.generation,
        fingerprint(
            {
                "model": snapshot.model,
                "ams_known": snapshot.ams_known,
                "ams_present": snapshot.ams_present,
                "external_known": snapshot.external_known,
                "nozzles": snapshot.nozzle_diameters,
                "fts": snapshot.fts,
                "fts_pending_confirmation": snapshot.fts_pending_confirmation,
                "left_tpu_firmware": snapshot.left_tpu_firmware,
                "backup_enabled": snapshot.backup_enabled,
                "incomplete": snapshot.incomplete,
                "sources": [
                    {k: v for k, v in asdict(source).items() if k not in ("remain", "variant", "declared_variant")}
                    for source in snapshot.sources
                ],
            }
        ),
    )


def revision_for(req, policy, snapshot):
    """The fingerprint stored as ``runtime.blocked_revision`` — it outlives the tick.

    It therefore uses the same portable revision the intent stores: for a captured
    source the hash, never the copy's mtime, or a restore would clear every
    recorded block and re-ask a question whose answer had not changed.

    The feed half is the policy-aware :func:`feed_signature` — the dispatch
    guard itself asks ``plan_holds`` — so the block a latching deferral records
    (``LATCHING_REASONS``) and the question the next preflight asks are the same
    question.
    """
    identity = req.source_identity
    return fingerprint(
        {
            "source": identity.revision() if identity else None,
            "policy": policy.fingerprint,
            "snapshot": feed_signature(policy, snapshot),
        }
    )


#: The refusals whose ``runtime.blocked_revision`` keeps a job from trying again
#: on the same evidence. Only a settle timeout: the printer never finished
#: reporting, so the next attempt waits for it to say something new. A refusal
#: because the feed CHANGED records the feed after the change — a state nobody
#: has tried — and must never park a job on it (spec dispatch-guard-follows-the-
#: plan Д3); the resolver's own refusals re-refuse before the latch is read. A
#: whitelist, so a reason added later does not latch by accident.
LATCHING_REASONS = frozenset({"feed_settle_timeout"})


async def preflight_item(db, item, printer_id, *, cache=None, prefer_lowest=None):
    # The captured source when there is one (m173): a snapshot-backed job is
    # answered from its blob, and the archive / library rows it was built from
    # may be gone — which is the whole point of having copied it (spec §7).
    descriptor = await item_descriptor(db, item)
    if descriptor is None:
        archive, library = await item_source(db, item)
        path = resolve_source_path(archive, library)
        raw_gcode = bool(path) and path.suffix.lower() == ".gcode"
    else:
        # The object is stored under its hash, so its own name would answer this
        # wrongly for a raw source; the format the capture verified is the answer.
        raw_gcode = descriptor.format == FORMAT_GCODE
    # These flags come from a server-created queue row, never request options.
    # Raw G-code and calibration deliberately have no normal 3MF requirement
    # contract.  They still must not send a *known* external-holder selection
    # through FTS: firmware rejects that physical topology.  Unknown/no mapping
    # stays exempt — this is a narrow wire-safety rule, not invented metadata.
    exempt_from_normal_routing = (item.is_calibration and item.calibration_session_id is not None) or (
        raw_gcode and item.source_auto_item_id is None
    )
    if exempt_from_normal_routing:
        snapshot = printer_manager.get_feed_snapshot(printer_id)
        if _has_explicit_external_mapping(item) and snapshot.fts:
            raise RoutingDeferred("fts_external_unsupported")
        if getattr(snapshot, "direct_rack", False) is True:
            await _direct_rack_exempt_check(db, item, snapshot)
        return None
    req = await read_item_requirements(db, item, cache)
    if req.status != "ok":
        raise RoutingDeferred(req.reason or "source_unreadable")
    policy = queue_policy(item)
    saved = decode(item.filament_routing, {})
    if not isinstance(saved, dict):
        raise RoutingDeferred("mapping_review_required")
    scope = saved.get("source_identity", {})
    if not isinstance(scope, dict) or not isinstance(saved.get("runtime", {}), dict):
        raise RoutingDeferred("mapping_review_required")
    # ⚠️ The ``{kind, id}`` scope is asked of a LEGACY row only, and that is the
    # narrowing this whole feature is for: it describes the ORIGINAL the intent was
    # written about, and it answered "source_changed" the moment a trashed library
    # file nulled the reference — refusing to dispatch a job whose bytes had not
    # moved. A captured job's scope question is its *revision* instead (below),
    # which compares content and not references. ``source_identity`` also records
    # ``queue_source_id`` for such a row, and it is deliberately NOT compared here:
    # ``queue_sources.id`` is reused by SQLite after a delete, so an id match is
    # weaker evidence than the hash that follows it.
    if (
        scope
        and descriptor is None
        and {k: scope.get(k) for k in ("kind", "id")} != source_scope(item.archive_id, item.library_file_id)
    ):
        raise RoutingDeferred("source_changed")
    if saved.get("printer_id") not in (None, printer_id):
        raise RoutingDeferred("mapping_review_required")
    if saved.get("resolved_plate_id") not in (None, 0, req.resolved_plate_id):
        raise RoutingDeferred("plate_selection_required")
    # ⚠️ Asked of EVERY row now, legacy and snapshot-backed alike — the writer
    # stamps a portable revision for a captured source (its hash), so the reader
    # no longer has to look away. What it still refuses to do is read a v1 stamp
    # of a snapshot's mtime as an identity; ``revision_refutes`` owns that rule
    # and the reason, and an unrecognised revision shape fails closed.
    if revision_refutes(scope.get("revision"), req.source_identity):
        raise RoutingDeferred("source_changed")
    # Reuse the established inventory/Spoolman ranking adapter, not the legacy
    # matcher. Ranking is a preference; source compatibility comes from the
    # fresh snapshot and the complete resolver below.
    snapshot, prefer_lowest, source_priority = await ranked_feed(db, printer_id, policy, prefer_lowest)
    revision = revision_for(req, policy, snapshot)
    exact_model = saved.get("exact_model", item.source_auto_item_id is not None)
    result = resolve_filament_routing(
        req,
        policy,
        snapshot,
        prefer_lowest=prefer_lowest,
        exact_model=exact_model,
        source_priority=source_priority,
        # A row without a routing intent re-reads its pins from item.ams_mapping,
        # which the scheduler overwrites with the plan — a twin would stick.
        allow_backup_twins=item.filament_routing is not None,
    )
    if result.plan is None:
        raise RoutingDeferred(result.reason or "mapping_review_required", revision=revision, params=result.params)
    # The latch: a refusal this job already recorded is not re-asked while the
    # evidence behind it is unchanged. ⚠️ Nothing CLEARS a stored block, and
    # nothing should: when the feed half of the revision changed shape — as it
    # did when it became policy-aware — an old block simply stops matching by
    # construction, and this job is re-evaluated on its next tick like any
    # other. An unconditional clear would instead re-dispatch every genuinely
    # blocked row on the first boot after such a change. Only
    # ``LATCHING_REASONS`` latch — see there.
    runtime = saved.get("runtime", {})
    if runtime.get("reason") in LATCHING_REASONS and runtime.get("blocked_revision") == revision:
        raise RoutingDeferred(runtime["reason"], revision=revision)
    return DispatchRoutingGuard(
        req,
        policy,
        result.plan,
        exact_model,
        revision,
        snapshot.generation,
        planned_nozzle_diameters(req, policy, result.plan, snapshot),
        requires_left_tpu_firmware_check(snapshot.model),
    )


def _mapped_trays(item) -> list[int]:
    """The trays an exempt row's own ``ams_mapping`` names (``-1`` and junk left out)."""
    mapping = getattr(item, "ams_mapping", None)
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (TypeError, ValueError):
            return []
    if not isinstance(mapping, list):
        return []
    return [v for v in mapping if isinstance(v, int) and not isinstance(v, bool) and v >= 0]


async def _direct_rack_exempt_check(db, item, snapshot) -> None:
    """A direct rack (H3C, P3C): what an exempt row (calibration, raw G-code) is still asked.

    * A row ``m275`` marked — its mapping was saved before H3C / P3C support,
      and it has no dialog to be confirmed in — waits until it is re-created
      (``rack_mapping_recreate``): sent as it is, 128 + k would go out as an
      AMS-HT that does not exist.
    * A calibration row: the hotend each mapped tray leads to must still have the
      session's nozzle diameter and flow (and a calibrated dock) — it may have been
      swapped while the row waited (``filament_routing.calibration_hotend_refusal``).
    """
    saved = decode(item.filament_routing, {})
    if isinstance(saved, dict) and saved.get("review_required") is True:
        raise RoutingDeferred("rack_mapping_recreate")
    if not (item.is_calibration and item.calibration_session_id is not None):
        return
    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.filament_routing import calibration_hotend_refusal

    session = await db.get(CalibrationSession, item.calibration_session_id)
    if session is None:
        return
    for tray in _mapped_trays(item) or [None]:
        code = calibration_hotend_refusal(snapshot, tray, session.nozzle_diameter, session.nozzle_volume_type)
        if code is not None:
            raise RoutingDeferred(code)


def _has_explicit_external_mapping(item) -> bool:
    """Whether an exempt item explicitly selected Bambu's virtual tray.

    ``-1`` is intentionally not treated as external: it is also the on-wire
    marker for an unresolved slot.  Only the durable queue values 254/255
    prove that an operator selected an external holder.
    """
    mapping = getattr(item, "ams_mapping", None)
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (TypeError, ValueError):
            return False
    return isinstance(mapping, list) and any(is_external_id(slot) for slot in mapping)


async def ranked_feed(db, printer_id, policy, prefer_lowest=None):
    """One ranking adapter for the dialog preview and the eventual dispatch."""
    from backend.app.services.print_scheduler import scheduler

    if prefer_lowest is None:
        prefer_lowest = await scheduler._get_bool_setting(db, "prefer_lowest_filament", default=True)
    snapshot = printer_manager.get_feed_snapshot(printer_id)
    source_priority = None
    # A pinned job needs a ranking only when a pinned slot is empty and its
    # backup twins compete; otherwise it is one Spoolman read per bound slot on
    # every preflight for nothing (final review).
    present = {source.id for source in snapshot.sources}
    ranking_matters = policy.mode == "auto" or any(
        pin.get("source_id") not in present for pin in policy.physical_pins.values()
    )
    if prefer_lowest and snapshot.backup_enabled is not False and ranking_matters:
        loaded = [
            {
                "ams_id": source.id if source.id >= 128 else source.id // 4,
                "tray_id": 0 if source.id >= 128 else source.id % 4,
                "global_tray_id": source.id,
                "is_external": source.kind == "external",
                "remain": source.remain,
            }
            for source in snapshot.sources
        ]
        remaining = await scheduler._build_inventory_remain_overrides(db, printer_id, loaded)
        source_priority = {
            source["global_tray_id"]: scheduler._prefer_lowest_sort_key(source, remaining) for source in loaded
        }
        snapshot = printer_manager.get_feed_snapshot(printer_id)
    return snapshot, prefer_lowest, source_priority


async def settle_plan(
    guard,
    printer_id: int,
    *,
    raise_if_cancelled: Callable[[], None] = lambda: None,
    timeout: float = FEED_SETTLE_TIMEOUT,
    poll: float = FEED_SETTLE_POLL,
    converge: float = FEED_SETTLE_CONVERGE,
    on_wait: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """Before the final check: wait — once, bounded — until the prepared plan holds.

    ``on_wait(reason)`` is awaited once, when the wait turns out to be for a
    planned slot the operator has emptied — the dispatch toast then says what
    the start is waiting for instead of «Starting print…».

    One deadline for everything that can put a prepared print here (spec
    dispatch-guard-follows-the-plan Д6):
    - the MQTT session changed since preparation — its feed cache starts empty,
      so the new session is asked for a full report first (2026-09-24, two A1 mini);
    - a planned slot is empty — an operator mid-swap;
    - a planned slot holds something that does not fit — BamDude's own writes
      after a spool goes in (the pre-config replay in ``on_ams_change``, assign,
      ``publish_slot_plan``) pass the tray through intermediate states, so this
      gets the short ``converge`` window rather than a refusal.

    Returns whether the session changed: the pre-start K bind went to the old
    one and is the caller's to send again. Raises ``feed_settle_timeout`` when a
    changed session never completed its report (latched — see
    ``LATCHING_REASONS``) and ``planned_source_empty`` when a planned slot stayed
    empty; any other refusal is ``final_guard``'s to name. The wait sits BEFORE
    that check, so nothing awaits between a passed check and the publish. A
    healthy printer on the same session whose plan holds returns at once — no
    pushall.
    """
    if guard is None:
        return False
    snapshot = printer_manager.get_feed_snapshot(printer_id)
    session_changed = not snapshot.connected or snapshot.generation != guard.generation
    if not session_changed and plan_holds(guard, snapshot)[0]:
        return False
    if session_changed:
        printer_manager.request_status_update(printer_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    converge_until = None
    reason = None
    told = False
    while True:
        raise_if_cancelled()
        snapshot = printer_manager.get_feed_snapshot(printer_id)
        now = loop.time()
        if not session_changed and (not snapshot.connected or snapshot.generation != guard.generation):
            session_changed = True
            printer_manager.request_status_update(printer_id)
        # A new session's feed cache starts empty (the client resets it with the
        # generation), so a complete feed on it can only come from its own reports.
        reported = snapshot.connected and snapshot.ams_known and snapshot.external_known and not snapshot.incomplete
        if reported or not session_changed:
            holds, reason, unknown = plan_holds(guard, snapshot)
            if holds:
                return session_changed
            if reason == "planned_source_empty" and on_wait is not None and not told:
                told = True
                await on_wait(reason)
            if not unknown:
                # Loaded and known not to fit: a separately reported fact or our
                # own slot write may still be on its way. A bounded grace; a real
                # difference is final_guard's to refuse.
                if converge_until is None:
                    converge_until = min(deadline, now + converge)
                if now >= converge_until:
                    return session_changed
        if now >= deadline:
            if session_changed and not reported:
                raise RoutingDeferred(
                    "feed_settle_timeout", revision=revision_for(guard.requirements, guard.policy, snapshot)
                )
            if reason == "planned_source_empty":
                raise RoutingDeferred("planned_source_empty")
            return session_changed
        await asyncio.sleep(poll)


async def final_guard(guard, printer_id, *, bind_generation: int | None = None):
    """After all preparatory awaits: the prepared plan still holds, or there is no start.

    A different plan is a new attempt, never a swapped mapping — the mapping
    never changes after preflight (inv-complete-routing-before-publish). A
    reconnect alone is not a changed plan once the new session reported the
    plan's own slots from scratch (``settle_plan`` waited for that); what the
    guard asks is ``plan_holds`` — the plan's slots under the job's own rule.
    Its refusals carry no revision, so none of them parks a queue row.

    ``bind_generation`` is the MQTT session the pre-start K bind was sent on
    (``background_dispatch._rebind_if_moved``). A session that changed since —
    during this function's own source probe — would start the print with no K
    selected, so it is a refusal, and ``validate`` then holds the publish to the
    very session confirmed here.
    """
    if guard is None:
        return None
    identity = guard.requirements.source_identity
    try:
        current = await probe_identity(identity)
    except SourceUnavailable as exc:
        raise RoutingDeferred(exc.reason) from exc
    if current != identity:
        raise RoutingDeferred("source_changed")
    snapshot = printer_manager.get_feed_snapshot(printer_id)
    if bind_generation is not None and snapshot.generation != bind_generation:
        raise RoutingDeferred("printer_reconnected")
    holds, reason, _unknown = plan_holds(guard, snapshot)
    if not holds:
        raise RoutingDeferred(reason or "feed_state_changed")
    now = {source.id: source for source in snapshot.sources}
    refreshed_plan = replace(
        guard.plan,
        assignments={slot: now[old.id] for slot, old in guard.plan.assignments.items()},
        snapshot_marker=snapshot.marker,
    )
    refreshed = replace(
        guard,
        plan=refreshed_plan,
        revision=revision_for(guard.requirements, guard.policy, snapshot),
        generation=snapshot.generation,
    )
    refreshed.validate(
        snapshot, mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, plate_id=guard.plan.resolved_plate_id
    )
    return refreshed
