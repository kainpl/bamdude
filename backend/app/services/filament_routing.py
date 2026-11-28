"""Complete filament assignment, independent of schedulers, routes and MQTT."""

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from backend.app.services.printer_feed_snapshot import FeedSource, PrinterFeedSnapshot
from backend.app.utils.direct_rack import MANUAL_FEED, PRELOAD, hotend_matches, hotend_spec_matches, retracts_itself
from backend.app.utils.filament_types import (
    SUPPORT_SLOT_TYPES,
    canonical_filament_type,
    filament_types_compatible,
    routing_filament_type,
)
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.printer_configs import requires_left_tpu_firmware_check
from backend.app.utils.printer_models import is_dual_nozzle_model, normalize_model_name
from backend.app.utils.tray_ids import EXTERNAL_DEPUTY_ID, EXTERNAL_MAIN_ID

if TYPE_CHECKING:
    from backend.app.services.filament_requirements import PrintRequirements

FeedPolicy = Literal["auto", "ams_only", "external_only"]


class RoutingDeferred(Exception):
    """No publish occurred: current evidence cannot authorize this attempt."""

    def __init__(
        self,
        reason: str,
        *,
        revision: str | None = None,
        params: dict | None = None,
        detail: str | None = None,
        blocker_archive_id: int | None = None,
    ):
        self.reason = reason
        self.revision = revision
        #: Same facts a ``RoutingResult`` carries, for the deferral an operator
        #: reads on the queue row. Empty for the refusals that are about the
        #: source or the claim rather than about a channel.
        self.params = params or {}
        #: Which branch refused, for diagnostics only (``background_dispatch.CLAIM_DETAILS``); the public
        #: ``reason`` and the operator's sentence do not change with it. ``None`` where nobody knows.
        self.detail = detail
        #: The execution archive of the run that held the printer, when that is what refused.
        self.blocker_archive_id = blocker_archive_id
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
    #: Raw provenance: the policy-BLIND marker of the snapshot this plan was
    #: resolved against. It is not how the dispatch boundary decides whether the
    #: plan still stands — that is ``filament_preflight.plan_holds``, asked of the
    #: plan's own slots. It survives as the plan's stamp and as the auto-queue
    #: placement's baseline of last resort (``auto_queue_scheduler._feed_moved``).
    snapshot_marker: tuple[int, str]
    assignments: dict[int, FeedSource]
    color_matches: int
    #: Planned on a direct rack (H3C, P3C): the tray decides the PrintTool and
    #: ``use_ams`` follows Bambu Studio's rule (below).
    direct_rack: bool = False
    #: What the operator should know before this plan prints (a direct rack's
    #: manual feed, two sources on one PrintTool) — never a reason to wait.
    notices: tuple[dict, ...] = ()

    @property
    def remaining_rank(self) -> float:
        """Lower average reported fill wins; unknown readings sort after known ones."""
        levels = [source.remain for source in self.assignments.values()]
        if not levels or any(not 0 <= level <= 100 for level in levels):
            return -101
        return -sum(levels) / len(levels)

    @property
    def mapping(self) -> list[int]:
        # Padding exists only at this serialization boundary.
        return [self.assignments[i].id if i in self.assignments else -1 for i in range(1, max(self.assignments) + 1)]

    @property
    def use_ams(self) -> bool:
        if self.direct_rack:
            # Bambu Studio: false only when every mapped tray is 254 / 255
            # (``SelectMachine.cpp:2931-2953``) — a print from input 248 alone is
            # still ``use_ams: true``. Not ``layout.is_external``: that is upstream's form.
            return not all(s.id in (EXTERNAL_DEPUTY_ID, EXTERNAL_MAIN_ID) for s in self.assignments.values())
        return any(s.kind == "ams" for s in self.assignments.values())


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
    #: The plan's notices (``RoutingPlan.notices``), for the dialog and the queue row.
    notices: tuple[dict, ...] = ()


#: Sources listed in a refusal before it stops naming them one by one.
_LISTED_SOURCES = 4


def describe_source(material: str | None, variant: str | None) -> str:
    return f"{material or '?'} ({variant})" if variant else (material or "?")


def _refusal_params(sid: int, target_type: str, variant: str | None, snapshot: PrinterFeedSnapshot) -> dict:
    # The profile the rule READ — the declared one when an advertisement is
    # applied — or the sentence contradicts the verdict it explains.
    dual = is_dual_nozzle_model(snapshot.model)
    sources = sorted(snapshot.sources, key=lambda source: source.kind != "external")
    listed = []
    for source in sources[:_LISTED_SOURCES]:
        description = describe_source(source.material, source.rule_variant)
        if source.kind == "external":
            label = snapshot.layout.rack_label(source.id) or (
                "Ext-R" if source.id == 255 else "Ext-L" if dual else "Ext"
            )
            description = f"{label}: {description}"
        listed.append(description)
    loaded = ", ".join(listed)
    if len(sources) > _LISTED_SOURCES:
        loaded = f"{loaded}, +{len(sources) - _LISTED_SOURCES}"
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


def effective_slots(requirements: "PrintRequirements", policy: RoutingPolicy) -> list[dict] | None:
    """The channels a job asks for once its overrides have spoken.

    Each slot's ``type`` / ``tray_info_idx`` / ``color`` as the operator
    overrode them, plus ``strict`` (a forced colour). ``None`` when an override
    names a channel the plate does not use. Shared by routing and by the
    auto-queue's wake step (``offline_feed``), so both ask a printer for the
    same filament.
    """
    slots = [dict(f) for f in requirements.used_filaments]
    overrides = {o["slot_id"]: o for o in policy.filament_overrides}
    if set(overrides) - {slot["slot_id"] for slot in slots}:
        return None
    for slot in slots:
        override = overrides.get(slot["slot_id"], {})
        enriched_type = slot.get("filament_type")
        slot["type"] = (
            enriched_type
            if enriched_type in SUPPORT_SLOT_TYPES
            else routing_filament_type(slot["type"], slot.get("tray_info_idx"))
        )
        if override.get("type"):
            override_type = routing_filament_type(override["type"], override.get("tray_info_idx"))
            if not filament_types_compatible(override_type, slot["type"]):
                slot["tray_info_idx"] = override.get("tray_info_idx")
            slot["type"] = override_type
        if override.get("tray_info_idx"):
            slot["tray_info_idx"] = override["tray_info_idx"]
        if override.get("color"):
            slot["color"] = override["color"]
        slot["strict"] = policy.force_color_match or bool(override.get("force_color_match"))
        if "nozzle_diameter" not in slot and isinstance(getattr(requirements, "nozzle_constraints", None), dict):
            # A slice without per-filament hotend data (another slicer): the
            # project's diameter for its nozzle, flow any — what a direct rack's
            # hotend is checked against (spec N01 B §5.2). Not used elsewhere.
            project = _required_diameter(requirements, slot_nozzle(slot))
            if project is not None and project > 0:
                slot["project_nozzle_diameter"] = project
    return slots


def slot_nozzle(slot: dict) -> int:
    """The nozzle a channel prints from — 0 unless the file names another."""
    return slot.get("nozzle_id") if slot.get("nozzle_id") is not None else 0


def channel_nozzle_counts(slots: list[dict]) -> Counter:
    return Counter(slot_nozzle(slot) for slot in slots)


def feed_preconditions(
    requirements: "PrintRequirements",
    policy: RoutingPolicy,
    snapshot: PrinterFeedSnapshot,
    *,
    exact_model: bool,
    with_model: bool = True,
) -> RoutingResult | None:
    """What refuses a whole job before any channel is looked at.

    ``with_model=False`` skips the model comparison: ``model_compatibility``
    builds its matrix from the mirrored printer configs on first use, and the
    synchronous publish boundary (``DispatchRoutingGuard.validate``) may not
    touch a file. The model cannot change between the final check and publish.
    """
    if requirements.status != "ok":
        return RoutingResult("unknown", requirements.reason or "source_unreadable")
    if policy.review_required:
        return RoutingResult("unknown", "mapping_review_required")
    if not snapshot.connected:
        return RoutingResult("unknown", "printer_offline")
    if with_model:
        model = normalize_model_name(requirements.model)
        if not model or not snapshot.model:
            return RoutingResult("unknown", "model_unavailable")
        verdict = model_compatibility(model, snapshot.model)
        if (exact_model and verdict != "exact") or (not exact_model and verdict == "incompatible"):
            return RoutingResult("incompatible", "model_mismatch")
    if not snapshot.ams_known:
        # In particular, an external tray report does not prove AMS absence.
        # Single-nozzle wire encoding differs when no AMS is attached.
        return RoutingResult("unknown", "feed_state_unavailable")
    return None


def channel_refusal(
    requirements: "PrintRequirements", slot: dict, snapshot: PrinterFeedSnapshot
) -> RoutingResult | None:
    """What refuses one channel whatever is loaded: its nozzle diameter, or a forced colour it cannot name."""
    sid = slot["slot_id"]
    nozzle = slot_nozzle(slot)
    # A direct rack: the diameter is asked of the hotend each source leads to
    # (``source_fits``), not of one head — the head holds whichever was last fetched.
    diameter = None if snapshot.direct_rack else _required_diameter(requirements, nozzle)
    if diameter is not None:
        installed = snapshot.nozzle_diameters.get(nozzle)
        if not installed:
            return RoutingResult("unknown", "nozzle_state_unavailable", slots=(sid,))
        if diameter not in installed:
            return RoutingResult("incompatible", "nozzle_mismatch", slots=(sid,))
    if slot["strict"] and normalized_color(slot.get("color")) is None:
        return RoutingResult("unknown", "color_unavailable", slots=(sid,))
    return None


def rack_preconditions(requirements: "PrintRequirements", snapshot: PrinterFeedSnapshot) -> RoutingResult | None:
    """What refuses a whole job on a direct rack (H3C, P3C) — Bambu Studio's blocking statuses.

    ``SelectMachine::update_show_status``: a dynamic nozzle-map slice cannot be
    sent (it waits for a V1 reply that needs a Filament Track Switch); a moving
    rack waits; no hotend at all, a tool missing from its holder, or no dock
    docking-calibrated refuses. Facts the printer has not reported yet refuse
    nothing here — the per-channel rule waits on them instead.
    """
    if not snapshot.direct_rack:
        return None
    if requirements.nozzle_constraints.get("dynamic_nozzle_map"):
        return RoutingResult("incompatible", "dynamic_nozzle_map_unsupported")
    rack = snapshot.rack
    if rack.busy:
        return RoutingResult("unknown", "rack_busy")
    if not rack.known:
        return RoutingResult("unknown", "nozzle_state_unavailable")
    if not rack.any_hotend:
        return RoutingResult("incompatible", "rack_no_hotend")
    if rack.any_detached:
        tools = [h.print_tool for h in rack.hotends if h.detached]
        return RoutingResult("incompatible", "rack_tool_detached", params={"print_tools": ", ".join(map(str, tools))})
    if rack.any_dock_calibrated is False:
        return RoutingResult("incompatible", "rack_not_calibrated")
    return None


def _rack_source_fits(
    slot: dict, source: FeedSource, snapshot: PrinterFeedSnapshot, pin: dict | None
) -> tuple[bool, str | None, bool] | None:
    """A direct rack's part of ``source_fits``: Bambu Studio's candidate rule and hotend spec.

    The source must be bound to an existing hotend (``FeedSource.rack_addr``,
    ``utils/direct_rack.candidate_print_tools``) whose diameter and flow are the
    ones the filament was sliced for. An automatic plan takes no tray Studio's
    ``QuerySlotFeedBlock`` blocks (manual feed, filament from an external or the
    rack AMS already in the hotend); an operator's pin may, but never onto a
    hotend whose dock is not docking-calibrated. ``None``: nothing refused here.
    """
    hotend = snapshot.rack.hotend(source.rack_addr)
    if hotend is None or not hotend.exists:
        return False, None, False
    diameter = slot.get("nozzle_diameter") or slot.get("project_nozzle_diameter")
    if not hotend_matches(hotend, diameter, slot.get("volume_type")):
        return False, "rack_hotend_mismatch", False
    if pin is not None:
        if hotend.dock_calibrated is False:
            return False, "rack_not_calibrated", False
        return None
    if source.feed_block == PRELOAD:
        # A hotend's own rack slot that is not its bound one: only a pin takes it.
        return False, None, False
    if source.feed_block is not None:
        return False, ("rack_manual_feed_only" if source.feed_block == MANUAL_FEED else "rack_feed_blocked"), False
    return None


def rack_notices(plan_assignments: dict[int, FeedSource], snapshot: PrinterFeedSnapshot) -> tuple[dict, ...]:
    """What a direct-rack plan's operator should know (Bambu Studio's warnings that the plan keeps).

    ``manual_feed``: a pinned tray the printer will not feed by itself (or one a
    loaded hotend blocks) — the print will pause for a hand. ``shared_print_tool``:
    two different sources on one PrintTool where one cannot unload itself — the
    print will stop for a manual change (the owner's decision: as Studio, send).
    """
    notices: list[dict] = []
    if not snapshot.direct_rack:
        return tuple(notices)
    layout = snapshot.layout
    by_tool: dict[int, dict[int, FeedSource]] = {}
    for sid, source in sorted(plan_assignments.items()):
        if source.rack_addr is not None:
            by_tool.setdefault(source.rack_addr, {})[source.id] = source
        if source.feed_block is not None and source.feed_block != PRELOAD:
            notices.append(
                {
                    "code": "manual_feed" if source.feed_block == MANUAL_FEED else "feed_blocked",
                    "slot": sid,
                    "source": layout.rack_label(source.id) or str(source.id),
                    "print_tool": (source.rack_addr + 1) if source.rack_addr is not None else None,
                }
            )
    for addr, sources in sorted(by_tool.items()):
        if len(sources) > 1 and not all(retracts_itself(tid, layout) for tid in sources):
            notices.append(
                {
                    "code": "shared_print_tool",
                    "print_tool": addr + 1,
                    "sources": ", ".join(layout.rack_label(tid) or str(tid) for tid in sorted(sources)),
                }
            )
    return tuple(notices)


def calibration_hotend_refusal(
    snapshot: PrinterFeedSnapshot, tray: int | None, diameter: float | None, flow: str | None
) -> str | None:
    """Why a calibration print of ``tray`` may not go to a direct rack, or None.

    A calibration row is exempt from routing, and its archive carries no
    nozzle diameter, so the hotend the tray leads to is asked here — at the
    session's start and again at dispatch (the hotend may be swapped while the
    row waits): it must exist, have its dock docking-calibrated, and have the
    session's diameter and flow (``flow`` in our vocabulary). The rack's own
    blocking statuses come first.
    """
    if not snapshot.direct_rack:
        return None
    rack = snapshot.rack
    if rack.busy:
        return "rack_busy"
    if not rack.known:
        return "nozzle_state_unavailable"
    if not rack.any_hotend:
        return "rack_no_hotend"
    if rack.any_detached:
        return "rack_tool_detached"
    source = next((s for s in snapshot.sources if s.id == tray), None) if tray is not None else None
    hotend = rack.hotend(source.rack_addr) if source is not None else None
    # Exact flow: a calibration is of ONE hotend, so "Hybrid" (a slice's "several
    # flows") is no wildcard here — a TPU285 hotend, which the wizard has no flow
    # for, is refused rather than calibrated as something else.
    if hotend is None or (flow or "") != hotend.flow or not hotend_spec_matches(hotend, diameter, flow):
        return "rack_hotend_mismatch"
    if hotend.dock_calibrated is False:
        return "rack_not_calibrated"
    return None


def backup_twins(source_id: int, snapshot: PrinterFeedSnapshot) -> frozenset[int]:
    """The AMS slots the firmware would feed from instead of an EMPTY ``source_id``.

    Only with AMS Backup on (``backup_enabled is True`` — unknown is not
    consent), only while ``source_id`` is absent from the feed (a loaded pinned
    slot is never swapped: the firmware would not switch either), never an
    external holder. The group is the firmware's own (``filam_bak``), which the
    backup-compatibility emulation may have filled with differently coloured
    spools on purpose — the owner's choice (2026-09-30). Whether a twin can
    serve the channel is still ``source_fits``'s question.
    """
    if snapshot.backup_enabled is not True or any(s.id == source_id for s in snapshot.sources):
        return frozenset()
    layout = snapshot.layout
    if layout.direct_rack:
        # Never an external input (247-255 here), and never a tray on another
        # PrintTool: the tray decides the PrintTool, so a twin there is another hotend.
        tool = layout.print_tool(source_id)
        return frozenset(
            m
            for m in snapshot.backup_membership.get(source_id, ())
            if m != source_id and not layout.is_external(m) and layout.print_tool(m) == tool
        )
    return frozenset(m for m in snapshot.backup_membership.get(source_id, ()) if m != source_id and m < 254)


def _pin_holds(pin: dict, slot: dict, source: FeedSource, policy: RoutingPolicy) -> bool:
    """Whether a pinned channel may take ``source``.

    The pin records what its slot held when the operator chose it. Its nozzle
    binding and material always count. Its colour counts only when the colour
    is forced (П4, owner 2026-09-30) and its profile only when the profile is
    strict — each against the spool OR what was declared for it, because a pin
    captured before an advertisement holds the spool's values.

    A backup twin (another slot standing in for an empty pinned one) answers
    only the nozzle and the material: the recorded colour and profile describe
    the spool that ran out, and the job's own forced colour / strict profile
    were already asked of the twin by ``source_fits``.
    """
    if pin.get("nozzles") is not None and tuple(pin["nozzles"]) != source.nozzles:
        return False
    pinned_type = routing_filament_type(pin.get("type") or slot["type"], pin.get("tray_info_idx"))
    if not filament_types_compatible(pinned_type, source.material):
        return False
    if source.id != pin.get("source_id"):
        return True
    if slot["strict"]:
        expected = normalized_color(pin.get("color")) or normalized_color(slot.get("color"))
        seen = {normalized_color(source.color), normalized_color(source.declared_color)} - {None}
        if not expected or expected not in seen:
            return False
    # A pin is a PHYSICAL slot, not a profile, so the option governs this
    # comparison exactly as it governs the channel's own gate. With it on, a tray
    # whose profile id was re-tagged while its material, its colour and its
    # nozzle binding stayed put is still the tray the operator pointed at —
    # re-profiling a spool moves no filament. With it off the operator asked for
    # that exact profile, here too.
    expected_variant = pin.get("tray_info_idx")
    seen_variants = {v for v in (source.variant, source.declared_variant) if v}
    return (
        policy.allow_base_material_match
        or not expected_variant
        or not seen_variants
        or expected_variant in seen_variants
    )


def source_fits(
    slot: dict,
    source: FeedSource,
    policy: RoutingPolicy,
    snapshot: PrinterFeedSnapshot,
    *,
    nozzle: int,
    nozzle_counts: Counter,
    allowed: frozenset[int] | None,
    pin: dict | None,
    left_tpu_check: bool,
    explain: bool = False,
) -> tuple[bool, str | None, bool]:
    """Whether one feed source can serve one channel — the ONE copy of the rule.

    ``(fits, reason, unknown)``. ``reason`` is ``None`` for a source that is
    silently not a candidate (another feed kind, not an allowed slot, another
    material); otherwise it names why this source was turned down, and a caller
    walking several keeps the last one, as the resolver always has. ``unknown``
    means the answer can change once the printer reports more — a guard waits on
    it and never passes it.

    The resolver asks it of every source for every channel; the dispatch guard
    (``filament_preflight.plan_holds``) asks it of the sources a plan CHOSE.

    The owner's rule (2026-09-30): base material is the spool's, always; the
    profile counts only when strict and the colour only when forced, and both
    are read off what the operator DECLARED to the AMS when that is applied
    (``FeedSource.rule_variant`` / ``rule_color``). ``left_tpu_check`` is
    ``requires_left_tpu_firmware_check(model)`` answered by the caller once:
    its first call reads a file, which the publish boundary may not.
    """
    # A source this channel may not take (not the pinned slot, not one of its
    # backup twins) is no candidate whatever else is true of it — and its
    # reasons are not this channel's: an external spool on an FTS printer must
    # not lend its refusal to an empty pinned AMS slot (final review).
    if allowed is not None and source.id not in allowed:
        return False, None, False
    # Firmware (and Bambu Studio) refuse any external feed while FTS is
    # installed, including an external TPU Feed Assist path. Asked before the
    # feed-policy selection so external_only cannot turn a physical refusal
    # into an accidental bypass.
    if (snapshot.fts or snapshot.fts_pending_confirmation) and source.kind == "external":
        reason = "fts_external_unsupported" if snapshot.fts else "fts_state_unavailable"
        return False, reason, snapshot.fts_pending_confirmation
    if policy.feed_policy == "ams_only" and source.kind != "ams":
        return False, "feed_policy_mismatch" if explain else None, False
    if policy.feed_policy == "external_only" and source.kind != "external":
        return False, "feed_policy_mismatch" if explain else None, False
    if not filament_types_compatible(source.material, slot["type"]):
        return False, "material_mismatch" if explain else None, False
    variant = slot.get("tray_info_idx")
    if variant and source.rule_variant and variant != source.rule_variant and not policy.allow_base_material_match:
        return False, "variant_mismatch", False
    if snapshot.direct_rack:
        rack_verdict = _rack_source_fits(slot, source, snapshot, pin)
        if rack_verdict is not None:
            return rack_verdict
    if not source.nozzles:
        return False, "nozzle_state_unavailable", True
    if nozzle not in source.nozzles:
        return False, "nozzle_mismatch", False
    # This is deliberately the actual structured source material, not a
    # profile name and not a broad TPU-* family match. TPU-AMS is a different
    # material string in Bambu Studio's own check.
    if (
        nozzle == 1
        and left_tpu_check
        and canonical_filament_type(source.material) == "TPU"
        and snapshot.left_tpu_firmware is not True
    ):
        unknown = snapshot.left_tpu_firmware is None
        return False, ("tpu_left_firmware_unavailable" if unknown else "tpu_left_firmware_unsupported"), unknown
    # Each external input of a direct rack is its own PrintTool, so several
    # channels on extruder 0 do not share one input path there.
    if source.kind == "external" and nozzle_counts[nozzle] > 1 and not snapshot.direct_rack:
        return False, "feed_topology_mismatch", False
    color = normalized_color(source.rule_color)
    if slot["strict"] and color != normalized_color(slot.get("color")):
        return False, ("color_mismatch" if color else "color_unavailable"), color is None
    if pin and not _pin_holds(pin, slot, source, policy):
        return False, "mapping_review_required", False
    return True, None, False


def resolve_filament_routing(
    requirements: "PrintRequirements",
    policy: RoutingPolicy,
    snapshot: PrinterFeedSnapshot,
    *,
    prefer_lowest: bool = False,
    exact_model: bool = True,
    source_priority: dict[int, tuple] | None = None,
    allow_backup_twins: bool = True,
) -> RoutingResult:
    """A complete, injective plan for the job on this feed, or a typed refusal.

    ``allow_backup_twins=False`` for a row without a routing intent: its pins
    are re-read from ``item.ams_mapping``, and a twin written there would
    become its pin for good.
    """
    refusal = feed_preconditions(requirements, policy, snapshot, exact_model=exact_model) or rack_preconditions(
        requirements, snapshot
    )
    if refusal is not None:
        return refusal
    slots = effective_slots(requirements, policy)
    if slots is None:
        return RoutingResult("unknown", "override_slot_not_used")
    # Bambu Studio lets two filaments of a direct rack's plate take one tray
    # (``_run_hotend_rack_color_mapping``: slots may be shared); everywhere else a
    # plan names distinct sources.
    distinct = not snapshot.direct_rack
    nozzle_counts = channel_nozzle_counts(slots)
    left_tpu_check = requires_left_tpu_firmware_check(snapshot.model)
    present = {source.id for source in snapshot.sources}
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
        refusal = channel_refusal(requirements, slot, snapshot)
        if refusal is not None:
            return refusal
        colors[sid] = normalized_color(slot.get("color"))
        pin = policy.physical_pins.get(sid)
        if policy.mode == "pinned" and (pin is None or pin.get("source_id", -1) < 0):
            return RoutingResult("unknown", "mapping_review_required", slots=(sid,))
        allowed = None
        if pin:
            allowed = frozenset({pin["source_id"]})
            if allow_backup_twins:
                allowed |= backup_twins(pin["source_id"], snapshot)
        candidates = []
        unknown = False
        reason = "material_mismatch"
        # A direct rack: the PrintTools whose fitting tray only a hand can feed —
        # the refusal names them ("load PrintTool n, or pin the slot").
        held_tools: set[int] = set()
        held_reason = None
        for source in snapshot.sources:
            fits, why, unsure = source_fits(
                slot,
                source,
                policy,
                snapshot,
                nozzle=slot_nozzle(slot),
                nozzle_counts=nozzle_counts,
                allowed=allowed,
                pin=pin,
                left_tpu_check=left_tpu_check,
            )
            unknown |= unsure
            if why is not None:
                reason = why
            if why in ("rack_manual_feed_only", "rack_feed_blocked") and source.rack_addr is not None:
                held_tools.add(source.rack_addr + 1)
                # A tray only a hand can feed is named before one a loaded hotend blocks.
                held_reason = "rack_manual_feed_only" if why == "rack_manual_feed_only" else held_reason or why
            if fits:
                candidates.append(source)
        if not candidates:
            if pin and pin["source_id"] not in present:
                # The chosen slot is empty and no backup twin could stand in —
                # the one sentence that is true, whatever a twin was refused for.
                reason = "pinned_source_empty"
            elif held_reason is not None:
                # A fitting tray exists, only a hand can feed it: that is the sentence,
                # not whichever other tray was turned down last.
                reason = held_reason
            unknown |= (
                (not snapshot.ams_known and policy.feed_policy != "external_only")
                or (not snapshot.external_known and policy.feed_policy != "ams_only")
                or snapshot.incomplete
            )
            params = _refusal_params(sid, target_type, slot.get("tray_info_idx"), snapshot)
            if held_tools:
                params["print_tools"] = ", ".join(map(str, sorted(held_tools)))
            return RoutingResult("unknown" if unknown else "incompatible", reason, slots=(sid,), params=params)
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
        # В1 (owner, 2026-09-30): a colour the operator DECLARED for backup is
        # an exact match, so «lowest remain first» uses the leftovers.
        exact = int(colors[sid] is not None and colors[sid] == normalized_color(source.rule_color))
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
        available = {sid: [s for s in options[sid] if not distinct or s.id not in used] for sid in remaining}
        if any(not sources for sources in available.values()):
            return
        if distinct and len({s.id for sources in available.values() for s in sources}) < len(remaining):
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
    notices = rack_notices(best, snapshot)
    return RoutingResult(
        "compatible",
        notices=notices,
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
            sum(
                int(colors[sid] is not None and colors[sid] == normalized_color(s.rule_color))
                for sid, s in best.items()
            ),
            direct_rack=snapshot.direct_rack,
            notices=notices,
        ),
    )
