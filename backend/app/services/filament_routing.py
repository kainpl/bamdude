"""Complete filament assignment, independent of schedulers, routes and MQTT."""

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from backend.app.services.printer_feed_snapshot import FeedSource, PrinterFeedSnapshot
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.printer_models import is_gcode_compatible, normalize_model_name

if TYPE_CHECKING:
    from backend.app.services.filament_requirements import PrintRequirements

FeedPolicy = Literal["auto", "ams_only", "external_only"]


class RoutingDeferred(Exception):
    """No publish occurred: current evidence cannot authorize this attempt."""

    def __init__(self, reason: str, *, revision: str | None = None):
        self.reason = reason
        self.revision = revision
        super().__init__(reason)


def normalized_color(value: str | None) -> str | None:
    value = (value or "").lstrip("#").upper()
    if len(value) not in (6, 8) or any(c not in "0123456789ABCDEF" for c in value):
        return None
    return value[:6]


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RoutingPolicy:
    mode: Literal["auto", "pinned"] = "auto"
    feed_policy: FeedPolicy = "auto"
    force_color_match: bool = False
    filament_overrides: tuple[dict, ...] = ()
    physical_pins: dict[int, dict] = field(default_factory=dict)
    review_required: bool = False

    @property
    def fingerprint(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class RoutingPlan:
    printer_id: int
    resolved_plate_id: int
    source_revision: dict
    policy_fingerprint: str
    snapshot_marker: tuple[int, str]
    assignments: dict[int, FeedSource]
    color_matches: int

    @property
    def mapping(self) -> list[int]:
        # Padding exists only at this serialization boundary.
        return [self.assignments[i].id if i in self.assignments else -1 for i in range(1, max(self.assignments) + 1)]

    @property
    def use_ams(self) -> bool:
        return any(s.kind == "ams" for s in self.assignments.values())

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "printer": self.printer_id,
                "plate": self.resolved_plate_id,
                "source": self.source_revision,
                "policy": self.policy_fingerprint,
                "assignments": {
                    slot: {k: v for k, v in asdict(feed).items() if k != "remain"}
                    for slot, feed in self.assignments.items()
                },
            }
        )


@dataclass(frozen=True)
class RoutingResult:
    status: Literal["compatible", "incompatible", "unknown"]
    reason: str | None = None
    plan: RoutingPlan | None = None
    slots: tuple[int, ...] = ()


def _required_diameter(requirements, nozzle):
    values = requirements.nozzle_constraints.get("nozzle_diameter")
    if values is None:
        return None
    values = values if isinstance(values, list) else [values]
    physical = requirements.nozzle_constraints.get("physical_extruder_map") or [0]
    try:
        index = [int(n) for n in physical].index(nozzle)
        return float(values[index if len(values) > 1 else 0])
    except (ValueError, IndexError, TypeError):
        return -1


def resolve_filament_routing(
    requirements: "PrintRequirements",
    policy: RoutingPolicy,
    snapshot: PrinterFeedSnapshot,
    *,
    prefer_lowest: bool = False,
    exact_model: bool = True,
    source_priority: dict[int, tuple] | None = None,
) -> RoutingResult:
    if requirements.status != "ok":
        return RoutingResult("unknown", requirements.reason or "source_unreadable")
    if policy.review_required:
        return RoutingResult("unknown", "mapping_review_required")
    if not snapshot.connected:
        return RoutingResult("unknown", "printer_offline")
    model = normalize_model_name(requirements.model)
    if not model or not snapshot.model:
        return RoutingResult("unknown", "model_unavailable")
    if (exact_model and model != snapshot.model) or (
        not exact_model and not is_gcode_compatible(model, snapshot.model)
    ):
        return RoutingResult("incompatible", "model_mismatch")
    if not snapshot.ams_known:
        # In particular, an external tray report does not prove AMS absence.
        # Single-nozzle wire encoding differs when no AMS is attached.
        return RoutingResult("unknown", "feed_state_unavailable")
    slots = [dict(f) for f in requirements.used_filaments]
    overrides = {o["slot_id"]: o for o in policy.filament_overrides}
    if set(overrides) - {slot["slot_id"] for slot in slots}:
        return RoutingResult("unknown", "override_slot_not_used")
    for slot in slots:
        override = overrides.get(slot["slot_id"], {})
        if override.get("type"):
            if canonical_filament_type(override["type"]) != canonical_filament_type(slot["type"]):
                slot["tray_info_idx"] = override.get("tray_info_idx")
            slot["type"] = override["type"]
        if override.get("tray_info_idx"):
            slot["tray_info_idx"] = override["tray_info_idx"]
        if override.get("color"):
            slot["color"] = override["color"]
        slot["strict"] = policy.force_color_match or bool(override.get("force_color_match"))
    nozzle_counts = Counter(s.get("nozzle_id") if s.get("nozzle_id") is not None else 0 for s in slots)
    options: dict[int, list[FeedSource]] = {}
    colors = {}
    for slot in slots:
        sid = slot["slot_id"]
        nozzle = slot.get("nozzle_id") if slot.get("nozzle_id") is not None else 0
        diameter = _required_diameter(requirements, nozzle)
        if diameter is not None:
            installed = snapshot.nozzle_diameters.get(nozzle)
            if not installed:
                return RoutingResult("unknown", "nozzle_state_unavailable", slots=(sid,))
            if diameter not in installed:
                return RoutingResult("incompatible", "nozzle_mismatch", slots=(sid,))
        target_color = normalized_color(slot.get("color"))
        colors[sid] = target_color
        if slot["strict"] and target_color is None:
            return RoutingResult("unknown", "color_unavailable", slots=(sid,))
        pin = policy.physical_pins.get(sid)
        if policy.mode == "pinned" and (pin is None or pin.get("source_id", -1) < 0):
            return RoutingResult("unknown", "mapping_review_required", slots=(sid,))
        candidates = []
        unknown = False
        reason = "material_mismatch"
        for source in snapshot.sources:
            if policy.feed_policy == "ams_only" and source.kind != "ams":
                continue
            if policy.feed_policy == "external_only" and source.kind != "external":
                continue
            if pin and source.id != pin["source_id"]:
                continue
            if canonical_filament_type(source.material) != canonical_filament_type(slot["type"]):
                continue
            variant = slot.get("tray_info_idx")
            if variant and source.variant and variant != source.variant:
                reason = "variant_mismatch"
                continue
            if not source.nozzles:
                unknown = True
                reason = "nozzle_state_unavailable"
                continue
            if nozzle not in source.nozzles:
                reason = "nozzle_mismatch"
                continue
            if source.kind == "external" and nozzle_counts[nozzle] > 1:
                reason = "feed_topology_mismatch"
                continue
            color = normalized_color(source.color)
            if slot["strict"] and color != target_color:
                unknown |= color is None
                reason = "color_mismatch" if color else "color_unavailable"
                continue
            if pin:
                # Captured expectations allow an explicitly chosen colour that
                # differs from the slice. Legacy pins need a non-ambiguous match.
                expected_color = normalized_color(pin.get("color")) or target_color
                expected_type = pin.get("type") or slot["type"]
                expected_variant = pin.get("tray_info_idx")
                if (
                    (pin.get("nozzles") is not None and tuple(pin["nozzles"]) != source.nozzles)
                    or not expected_color
                    or not color
                    or color != expected_color
                    or canonical_filament_type(expected_type) != canonical_filament_type(source.material)
                    or (expected_variant and source.variant and expected_variant != source.variant)
                ):
                    reason = "mapping_review_required"
                    continue
            candidates.append(source)
        if not candidates:
            unknown |= (
                (not snapshot.ams_known and policy.feed_policy != "external_only")
                or (not snapshot.external_known and policy.feed_policy != "ams_only")
                or snapshot.incomplete
            )
            return RoutingResult("unknown" if unknown else "incompatible", reason, slots=(sid,))
        options[sid] = candidates
    # Most constrained first. Search complete assignments; a flexible channel
    # must not consume the only source of a pinned/strict one.
    order = sorted(options, key=lambda sid: (len(options[sid]), sid))
    prefer_lowest = prefer_lowest and snapshot.backup_enabled is not False

    ranks = (
        {source_id: rank for rank, source_id in enumerate(sorted(source_priority or {}, key=source_priority.get))}
        if source_priority
        else {}
    )
    max_penalty = max(102, len(ranks) + 1)

    def score(sid, source):
        exact = int(colors[sid] is not None and colors[sid] == normalized_color(source.color))
        remain = source.remain if 0 <= source.remain <= 100 else 101
        penalty = ranks.get(source.id, len(ranks)) if ranks else remain
        return exact * (max_penalty * len(order) + 1) - (penalty if prefer_lowest else 0)

    for sid in order:
        options[sid].sort(key=lambda source: (-score(sid, source), source.id))
    best_score = float("-inf")
    best = None

    def visit(index, chosen, used, total):
        nonlocal best, best_score
        remaining = order[index:]
        if not remaining:
            if total > best_score:
                best, best_score = dict(chosen), total
            return
        available = {sid: [s for s in options[sid] if s.id not in used] for sid in remaining}
        if any(not sources for sources in available.values()):
            return
        if len({s.id for sources in available.values() for s in sources}) < len(remaining):
            return
        if total + sum(max(score(sid, s) for s in sources) for sid, sources in available.items()) <= best_score:
            return
        sid = remaining[0]
        for source in available[sid]:
            chosen[sid] = source
            visit(index + 1, chosen, used | {source.id}, total + score(sid, source))
            chosen.pop(sid)

    visit(0, {}, set(), 0)
    if best is None:
        return RoutingResult("incompatible", "distinct_sources_required", slots=tuple(order))
    identity = requirements.source_identity
    return RoutingResult(
        "compatible",
        plan=RoutingPlan(
            snapshot.printer_id,
            requirements.resolved_plate_id,
            # One spelling of "which revision of the source is this plan about",
            # shared with the stored intent: a captured snapshot's hash, an
            # original's stat. This fingerprint never leaves the process, but the
            # mtime of a frozen copy is not an identity anywhere (spec §7).
            identity.revision() if identity else {},
            policy.fingerprint,
            snapshot.marker,
            best,
            sum(int(colors[sid] is not None and colors[sid] == normalized_color(s.color)) for sid, s in best.items()),
        ),
    )
