"""Read-only dispatch preflight and a synchronous guard at the MQTT boundary."""

from dataclasses import dataclass, replace

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


@dataclass(frozen=True)
class DispatchRoutingGuard:
    requirements: object
    policy: object
    plan: object
    exact_model: bool
    revision: str

    def validate(self, snapshot, *, mapping, use_ams, plate_id):
        """Must run under the client's routing lock, without an await before publish."""
        # Source I/O is checked by final_guard before this synchronous handoff.
        # Never stat a network mount while holding the MQTT telemetry lock.
        if not snapshot.connected or snapshot.marker != self.plan.snapshot_marker:
            raise RoutingDeferred("feed_state_changed", revision=revision_for(self.requirements, self.policy, snapshot))
        if mapping != self.plan.mapping or use_ams != self.plan.use_ams or plate_id != self.plan.resolved_plate_id:
            raise RoutingDeferred("mapping_review_required")


def revision_for(req, policy, snapshot):
    """The fingerprint stored as ``runtime.blocked_revision`` — it outlives the tick.

    It therefore uses the same portable revision the intent stores: for a captured
    source the hash, never the copy's mtime, or a restore would clear every
    recorded block and re-ask a question whose answer had not changed.
    """
    identity = req.source_identity
    return fingerprint(
        {
            "source": identity.revision() if identity else None,
            "policy": policy.fingerprint,
            "snapshot": snapshot.marker,
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
    if item.is_calibration and item.calibration_session_id is not None:
        return None
    if raw_gcode and item.source_auto_item_id is None:
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
    revision = revision_for(req, policy, snapshot)
    exact_model = saved.get("exact_model", item.source_auto_item_id is not None)
    result = resolve_filament_routing(
        req, policy, snapshot, prefer_lowest=prefer_lowest, exact_model=exact_model, source_priority=source_priority
    )
    if result.plan is None:
        raise RoutingDeferred(result.reason or "mapping_review_required", revision=revision, params=result.params)
    if saved.get("runtime", {}).get("blocked_revision") == revision:
        raise RoutingDeferred(saved["runtime"].get("reason", "feed_state_changed"), revision=revision)
    return DispatchRoutingGuard(req, policy, result.plan, exact_model, revision)


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
    refreshed = replace(guard, plan=refreshed_plan, revision=revision)
    refreshed.validate(
        snapshot, mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, plate_id=guard.plan.resolved_plate_id
    )
    return refreshed
