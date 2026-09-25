"""Read-only dispatch preflight and a synchronous guard at the MQTT boundary."""

import asyncio
import json
from collections.abc import Callable
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
from backend.app.services.filament_routing import RoutingDeferred, fingerprint, resolve_filament_routing
from backend.app.services.printer_manager import printer_manager
from backend.app.services.source_io import SourceUnavailable

#: How long a prepared attempt waits for a reconnected printer's first complete
#: feed report before it is refused (spec direct-print-silent-cancel §4.3).
FEED_SETTLE_TIMEOUT = 60.0
FEED_SETTLE_POLL = 1.0
#: Once the new session's feed looks complete but still differs from the prepared
#: one, how long to let separately reported facts (the FTS confirmation, a nozzle
#: diameter, a tag) catch up before handing the difference to ``final_guard``.
FEED_SETTLE_CONVERGE = 5.0


@dataclass(frozen=True)
class DispatchRoutingGuard:
    requirements: object
    policy: object
    plan: object
    exact_model: bool
    revision: str
    #: What "the feed has not moved" meant when this guard was built, read under
    #: this job's own policy. Recorded rather than re-derived from the plan,
    #: because the plan describes the trays it CHOSE and the feed is the whole
    #: of what was on offer — a spool pulled out of an unassigned slot still
    #: changes what a re-run of the resolver would answer.
    snapshot_signature: tuple[int, str]

    def validate(self, snapshot, *, mapping, use_ams, plate_id):
        """Must run under the client's routing lock, without an await before publish."""
        # Source I/O is checked by final_guard before this synchronous handoff.
        # Never stat a network mount while holding the MQTT telemetry lock.
        # ``feed_signature`` is pure arithmetic over a snapshot already in hand,
        # so this stays as awaitless as the raw marker comparison it replaced.
        if not snapshot.connected or feed_signature(self.policy, snapshot) != self.snapshot_signature:
            raise RoutingDeferred("feed_state_changed", revision=revision_for(self.requirements, self.policy, snapshot))
        if mapping != self.plan.mapping or use_ams != self.plan.use_ams or plate_id != self.plan.resolved_plate_id:
            raise RoutingDeferred("mapping_review_required")


def feed_signature(policy, snapshot) -> tuple[int, str]:
    """Whether the feed has moved, asked under one job's own policy.

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
    one, a lost AMS or a reconnect all still move this.

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
                    {k: v for k, v in asdict(source).items() if k not in ("remain", "variant")}
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

    The feed half is the policy-aware :func:`feed_signature`, the same one the
    guard compares, so the block a deferral records and the question the next
    preflight asks are the same question.
    """
    identity = req.source_identity
    return fingerprint(
        {
            "source": identity.revision() if identity else None,
            "policy": policy.fingerprint,
            "snapshot": feed_signature(policy, snapshot),
        }
    )


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
        if _has_explicit_external_mapping(item) and printer_manager.get_feed_snapshot(printer_id).fts:
            raise RoutingDeferred("fts_external_unsupported")
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
        req, policy, snapshot, prefer_lowest=prefer_lowest, exact_model=exact_model, source_priority=source_priority
    )
    if result.plan is None:
        raise RoutingDeferred(result.reason or "mapping_review_required", revision=revision, params=result.params)
    # The latch: a refusal this job already recorded is not re-asked while the
    # evidence behind it is unchanged. ⚠️ Nothing CLEARS a stored block, and
    # nothing should: when the feed half of the revision changed shape — as it
    # did when it became policy-aware — an old block simply stops matching by
    # construction, and this job is re-evaluated on its next tick like any
    # other. An unconditional clear would instead re-dispatch every genuinely
    # blocked row on the first boot after such a change.
    if saved.get("runtime", {}).get("blocked_revision") == revision:
        raise RoutingDeferred(saved["runtime"].get("reason", "feed_state_changed"), revision=revision)
    return DispatchRoutingGuard(req, policy, result.plan, exact_model, revision, feed_signature(policy, snapshot))


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
    return isinstance(mapping, list) and any(
        isinstance(slot, int) and not isinstance(slot, bool) and slot >= 254 for slot in mapping
    )


async def ranked_feed(db, printer_id, policy, prefer_lowest=None):
    """One ranking adapter for the dialog preview and the eventual dispatch."""
    from backend.app.services.print_scheduler import scheduler

    if prefer_lowest is None:
        prefer_lowest = await scheduler._get_bool_setting(db, "prefer_lowest_filament", default=True)
    snapshot = printer_manager.get_feed_snapshot(printer_id)
    source_priority = None
    if prefer_lowest and snapshot.backup_enabled is not False and policy.mode == "auto":
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


def _settled(snapshot, prepared_generation: int) -> bool:
    """A NEW session whose feed is complete — evidence gathered from scratch.

    The client empties the feed cache with every new generation, so a complete
    feed here can only have come from reports on this session.
    """
    return (
        snapshot.connected
        and snapshot.generation != prepared_generation
        and snapshot.ams_known
        and snapshot.external_known
        and not snapshot.incomplete
    )


async def settle_feed(
    guard,
    printer_id: int,
    *,
    raise_if_cancelled: Callable[[], None] = lambda: None,
    timeout: float = FEED_SETTLE_TIMEOUT,
    poll: float = FEED_SETTLE_POLL,
    converge: float = FEED_SETTLE_CONVERGE,
) -> bool:
    """Before the final check: if the session changed since preparation, wait for its first complete report.

    A reconnect mid-upload used to refuse every prepared print, because the new
    session starts with an empty feed cache (2026-09-24, two A1 mini). Now the
    attempt asks for a full report and waits — bounded — so ``final_guard`` can
    compare the FRESH feed with the prepared one. The comparison, and the refusal
    when the content differs, stay ``final_guard``'s. The wait sits BEFORE that
    check, so nothing awaits between a passed check and the publish.

    Returns whether the session changed: whatever was sent to the old one before
    the wait (the pre-start calibration bind) is the caller's to send again. A
    healthy printer (same generation, connected) returns ``False`` at once — no
    pushall.
    """
    if guard is None:
        return False
    prepared_generation, prepared_content = guard.snapshot_signature
    snapshot = printer_manager.get_feed_snapshot(printer_id)
    if snapshot.connected and snapshot.generation == prepared_generation:
        return False
    printer_manager.request_status_update(printer_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    converge_until = None
    while True:
        raise_if_cancelled()
        snapshot = printer_manager.get_feed_snapshot(printer_id)
        if _settled(snapshot, prepared_generation):
            if feed_signature(guard.policy, snapshot)[1] == prepared_content:
                return True
            # Complete-looking but different: a separately reported fact may still
            # be on its way. A bounded grace; a real difference is final_guard's.
            if converge_until is None:
                converge_until = min(deadline, loop.time() + converge)
            if loop.time() >= converge_until:
                return True
        elif loop.time() >= deadline:
            raise RoutingDeferred(
                "feed_settle_timeout", revision=revision_for(guard.requirements, guard.policy, snapshot)
            )
        await asyncio.sleep(poll)


async def final_guard(guard, printer_id):
    """Refresh after all preparatory awaits. A different complete plan needs a new attempt."""
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
    revision = revision_for(guard.requirements, guard.policy, snapshot)
    result = resolve_filament_routing(guard.requirements, guard.policy, snapshot, exact_model=guard.exact_model)
    if result.plan is None:
        raise RoutingDeferred(result.reason or "feed_state_changed", revision=revision, params=result.params)
    # A reconnect alone is not a changed feed once the new session has reported
    # the same CONTENT from scratch (spec direct-print-silent-cancel §4.3): the
    # cache empties with the generation, so equal content here is fresh evidence,
    # and the plan fingerprint carries no generation. Anything physical that moved
    # — a tag, a material, a colour, the topology, completeness — still defers.
    # The signature ignores remain (and variant under ON), never physical
    # identity. The synchronous publish boundary keeps the strict comparison.
    if feed_signature(guard.policy, snapshot)[1] != guard.snapshot_signature[1]:
        raise RoutingDeferred("feed_state_changed", revision=revision)
    # Keep the prepared assignment if it remains valid. Remain-only updates
    # cannot select another spool after calibration/colour attribution ran.
    selected = {
        slot: next((s for s in snapshot.sources if s.id == old.id), None)
        for slot, old in guard.plan.assignments.items()
    }
    if any(s is None for s in selected.values()):
        raise RoutingDeferred("feed_state_changed", revision=revision)
    refreshed_plan = replace(guard.plan, assignments=selected, snapshot_marker=snapshot.marker)
    if refreshed_plan.fingerprint != guard.plan.fingerprint:
        raise RoutingDeferred("feed_state_changed", revision=revision)
    refreshed = replace(
        guard, plan=refreshed_plan, revision=revision, snapshot_signature=feed_signature(guard.policy, snapshot)
    )
    refreshed.validate(
        snapshot, mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, plate_id=guard.plan.resolved_plate_id
    )
    return refreshed
