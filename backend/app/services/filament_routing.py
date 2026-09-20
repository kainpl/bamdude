"""Complete filament assignment, independent of schedulers, routes and MQTT."""

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from backend.app.services.printer_feed_snapshot import FeedSource, PrinterFeedSnapshot
from backend.app.utils.filament_types import filament_types_compatible
from backend.app.utils.printer_models import is_gcode_compatible, normalize_model_name

if TYPE_CHECKING:
    from backend.app.services.filament_requirements import PrintRequirements

FeedPolicy = Literal["auto", "ams_only", "external_only"]


class RoutingDeferred(Exception):
    """No publish occurred: current evidence cannot authorize this attempt."""

    def __init__(self, reason: str, *, revision: str | None = None, params: dict | None = None):
        self.reason = reason
        self.revision = revision
        #: Same facts a ``RoutingResult`` carries, for the deferral an operator
        #: reads on the queue row. Empty for the refusals that are about the
        #: source or the claim rather than about a channel.
        self.params = params or {}
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
    allow_base_material_match: bool = True
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
    #: Whether a profile id is part of what "the same plan" means. The plan does
    #: not carry the policy, and it is asked this question long after the policy
    #: has gone out of scope — at the dispatcher's final refresh, comparing a
    #: plan against itself. Defaults to the strict reading so anything that
    #: builds a plan without answering keeps the behaviour it had.
    variant_sensitive: bool = True

    @property
    def mapping(self) -> list[int]:
        # Padding exists only at this serialization boundary.
        return [self.assignments[i].id if i in self.assignments else -1 for i in range(1, max(self.assignments) + 1)]

    @property
    def use_ams(self) -> bool:
        return any(s.kind == "ams" for s in self.assignments.values())

    @property
    def fingerprint(self) -> str:
        # ``remain`` is never part of this: a spool that lost a gram during the
        # upload is the same spool. With the base-material option on, neither is
        # ``variant`` — re-profiling a tray moves no filament, so a plan made
        # against it is still the plan. Everything PHYSICAL stays either way:
        # the tag on the spool, its material, its colour, its nozzle binding,
        # which feed it is and which slot it sits in.
        volatile = ("remain",) if self.variant_sensitive else ("remain", "variant")
        return fingerprint(
            {
                "printer": self.printer_id,
                "plate": self.resolved_plate_id,
                "source": self.source_revision,
                "policy": self.policy_fingerprint,
                "assignments": {
                    slot: {k: v for k, v in asdict(feed).items() if k not in volatile}
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
    #: Facts behind a per-channel refusal, for the sentence the operator reads:
    #: ``slot``, ``wanted`` and ``loaded``. A refusal that names neither the
    #: channel nor what either side is holding cannot be acted on — 24 printers
    #: refused one ABS plate for weeks under a sentence blaming the material,
    #: while every one of them had ABS and differed only in the profile id.
    params: dict = field(default_factory=dict)


#: Sources listed in a refusal before it stops naming them one by one.
_LISTED_SOURCES = 4


def describe_source(material: str | None, variant: str | None) -> str:
    return f"{material or '?'} ({variant})" if variant else (material or "?")


def _refusal_params(sid: int, target_type: str, variant: str | None, sources) -> dict:
    listed = [describe_source(s.material, s.variant) for s in sources]
    loaded = ", ".join(listed[:_LISTED_SOURCES])
    if len(listed) > _LISTED_SOURCES:
        loaded = f"{loaded}, +{len(listed) - _LISTED_SOURCES}"
    return {"slot": sid, "wanted": describe_source(target_type, variant), "loaded": loaded}


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
            if not filament_types_compatible(override["type"], slot["type"]):
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
        # The material this channel needs is the one the FILE declares, after
        # canonicalisation (``filament_types_compatible``). It is read the same
        # way whatever the policy says: "allow base material match" is the
        # operator's answer to «any ABS will do», and what it governs is whether
        # a profile ID may veto a material that already matches — below, and in
        # the pin clause further down. It is not a second source for the material
        # itself, and the paragraph below is why it must not become one again.
        #
        # The comparison used to switch to ``filament_type`` — the family this
        # channel's ``tray_info_idx`` resolves to in the catalogue — whenever
        # that resolved. Two things were wrong with that. It inverted the option
        # on exactly the files it was written for: a custom slicer preset id
        # ("Pa240002") resolves to no family, the option read as OFF and the
        # strict id comparison it exists to suppress came back on. Measured on a
        # 24-printer farm (2026-09-20): every machine holding ABS refused an ABS
        # plate, naming the filament TYPE. And where the family DID resolve it
        # could contradict the plate — an id re-pointed in the cloud, a stale
        # row, an id another vendor reused — and the printer extrudes what the
        # slicer sliced for, not what a lookup table says the id means.
        #
        # So the catalogue neither permits, forbids nor substitutes a material
        # here, and there is no whitelist of materials this applies to: ABS
        # prints on ABS, and PVB on PVB, on the same terms.
        #
        # ⚠️ An unresolvable family is the NORMAL case on a working farm, not an
        # edge: the catalogue is filled through one operator's cloud link, while
        # the plates arrive from several people's slicers. Everyone else's
        # presets are ids this install has never seen and never will. Anything
        # that makes routing depend on resolving them strands those plates.
        target_type = slot["type"]
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
            if not filament_types_compatible(source.material, target_type):
                continue
            variant = slot.get("tray_info_idx")
            if variant and source.variant and variant != source.variant and not policy.allow_base_material_match:
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
                    or not filament_types_compatible(
                        expected_type,
                        source.material,
                    )
                    # A pin is a PHYSICAL slot, not a profile, so the option
                    # governs this comparison exactly as it governs the gate
                    # above. With it on, a tray whose profile id was re-tagged
                    # while its material, its colour and its nozzle binding
                    # stayed put is still the tray the operator pointed at —
                    # re-profiling a spool moves no filament. With it off the
                    # operator asked for that exact profile, here too.
                    or (
                        not policy.allow_base_material_match
                        and expected_variant
                        and source.variant
                        and expected_variant != source.variant
                    )
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
            return RoutingResult(
                "unknown" if unknown else "incompatible",
                reason,
                slots=(sid,),
                params=_refusal_params(sid, target_type, slot.get("tray_info_idx"), snapshot.sources),
            )
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
            not policy.allow_base_material_match,
        ),
    )
