"""Plain routing observations. Display text never identifies an incident."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.app.i18n import t
from backend.app.services.filament_routing import _required_diameter, effective_slots, normalized_color, slot_nozzle
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.printer_models import normalize_model_name

ROUTINE_REASONS = frozenset(
    {
        "printer_busy",
        "printer_waking",
        "source_changed",
        "nozzle_state_unavailable",
        "feed_state_unavailable",
        "fts_state_unavailable",
        "tpu_left_firmware_unavailable",
        "model_unavailable",
        "color_unavailable",
        "placement_conflict",
        "swap_request_refused",
        "source_check_busy",
        "source_copy_busy",
        # A direct rack (H3C, P3C) that is moving: it stops by itself.
        "rack_busy",
    }
)
TERMINAL_REASONS = frozenset({"source_unreadable", "source_timeout"})
ACTIONABLE_REASONS = frozenset(
    {
        "no_eligible_printer",
        "incompatible_target",
        "compatible_fallback_disabled",
        "mapping_review_required",
        "printer_offline",
        "printer_off_missing_filament",
        "model_mismatch",
        "nozzle_mapping_unavailable",
        "nozzle_mismatch",
        "color_mismatch",
        "material_mismatch",
        "variant_mismatch",
        "feed_topology_mismatch",
        # The job's own filament-source setting (AMS only / external only) excludes the slot: only changing it helps.
        "feed_policy_mismatch",
        "fts_external_unsupported",
        "tpu_left_firmware_unsupported",
        "distinct_sources_required",
        "override_slot_not_used",
        "previous_print_failed",
        # The printer's queue is halted (paused/error after a failed or cancelled print): only Resume helps.
        "queue_halted",
        # A direct-connected rack (H3C, P3C) refusing a command it does not take yet
        # (automatic calibration): only another printer helps.
        "direct_rack_unsupported",
        # A direct rack (H3C, P3C): Bambu Studio's blocking statuses, each needing a person
        # (a hotend installed, a tool put back, the docks calibrated, a hand to feed or
        # unload a PrintTool, a re-slice, a job re-created).
        "rack_no_hotend",
        "rack_tool_detached",
        "rack_not_calibrated",
        "rack_hotend_mismatch",
        "rack_manual_feed_only",
        "rack_feed_blocked",
        "dynamic_nozzle_map_unsupported",
        "rack_mapping_recreate",
        "pinned_source_empty",
        "invalid_plate_id",
        "invalid_plate_metadata",
        "ambiguous_plate_gcode",
        "plate_selection_required",
        "plate_gcode_missing",
        "plate_not_found",
        "filament_usage_unavailable",
        "filament_type_unavailable",
        "slice_info_missing",
        "invalid_project_settings",
        "invalid_printer_model",
        "routing_unclassified",
    }
)
KNOWN_REASONS = ROUTINE_REASONS | TERMINAL_REASONS | ACTIONABLE_REASONS


def freeze(value):
    """Canonical sets/maps of machine facts, never ORM instances or telemetry."""
    if isinstance(value, dict):
        return tuple(sorted((str(key), freeze(item)) for key, item in value.items()))
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(sorted({freeze(item) for item in value}, key=repr))
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value).isoformat()
    return value


@dataclass(frozen=True)
class WaitingFact:
    code: str
    printer_id: int | None = None
    params: tuple = ()
    message: str = field(default="", compare=False)
    printer_name: str = field(default="", compare=False)
    uncertain: bool = field(default=False, compare=False)

    @property
    def actionable(self):
        return not self.uncertain and self.code not in ROUTINE_REASONS | TERMINAL_REASONS

    @property
    def preserves_incident(self):
        # Waiting because a printer is occupied or its evidence is temporarily
        # unavailable does not prove that its previous obstruction disappeared.
        return self.uncertain or self.code in ROUTINE_REASONS or self.code == "printer_offline"

    def key(self, model, location):
        model = normalize_model_name(model) or model or "any"
        return model.casefold(), location, self.code, self.printer_id, self.params


def fact(code, printer_id=None, *, params=None, message="", printer_name="", uncertain=False):
    if code not in KNOWN_REASONS:
        # A future reason gets one diagnostic event; never expose a raw error.
        params = {
            "unclassified_code": code
            if isinstance(code, str) and len(code) <= 80 and code.isidentifier()
            else "unknown"
        }
        code = "routing_unclassified"
    return WaitingFact(code, printer_id, freeze(params or {}), message, printer_name, uncertain)


def routing_params(code, requirements, policy, snapshot, slots=()):
    """Facts used by the existing refusal, without a second solver pass.

    In particular, source names/revisions, remain, temperatures, and translated
    wanted/loaded strings never become notification identity.
    """
    if code in {"printer_offline", "feed_state_unavailable", "model_unavailable"}:
        return {}
    used = effective_slots(requirements, policy) or []
    if slots:
        used = [slot for slot in used if slot["slot_id"] in slots]
    wanted = [{"slot": slot["slot_id"], "material": canonical_filament_type(slot["type"])} for slot in used]
    params = {"wanted": wanted, "feed_policy": policy.feed_policy}
    if code in {"color_mismatch", "color_unavailable", "printer_off_missing_filament"}:
        for item, slot in zip(wanted, used, strict=True):
            if slot.get("strict"):
                item["color"] = normalized_color(slot.get("color"))
    if code == "variant_mismatch":
        for item, slot in zip(wanted, used, strict=True):
            item["variant"] = slot.get("tray_info_idx")
    if code in {"mapping_review_required", "pinned_source_empty"}:
        params["pins"] = policy.physical_pins
    if code in {"material_mismatch", "variant_mismatch", "color_mismatch", "color_unavailable"}:
        loaded = []
        wanted_materials = {item["material"] for item in wanted}
        for source in snapshot.sources:
            if code == "material_mismatch" or canonical_filament_type(source.material) not in wanted_materials:
                continue
            if policy.feed_policy != "auto" and source.kind != (
                "ams" if policy.feed_policy == "ams_only" else "external"
            ):
                continue
            if not any(slot_nozzle(slot) in source.nozzles for slot in used):
                continue
            item = {"material": canonical_filament_type(source.material)}
            if code == "variant_mismatch":
                item["variant"] = source.rule_variant
            if code.startswith("color_"):
                item["color"] = normalized_color(source.rule_color)
            loaded.append(item)
        params["loaded"] = loaded
    if code in {"nozzle_mismatch", "nozzle_state_unavailable", "feed_topology_mismatch", "distinct_sources_required"}:
        nozzles = {slot_nozzle(slot) for slot in used}
        params["required_nozzles"] = {n: _required_diameter(requirements, n) for n in nozzles}
        params["installed_nozzles"] = {n: snapshot.nozzle_diameters.get(n) for n in nozzles}
        if code != "nozzle_mismatch":
            params["feeds"] = [
                (source.id, source.kind, tuple(n for n in source.nozzles if n in nozzles))
                for source in snapshot.sources
                if set(source.nozzles) & nozzles
            ]
    return params


def file_params(item, requirements):
    """Observed source identity, never the job's display name or job ID."""
    identity = getattr(requirements, "source_identity", None)
    sha256 = getattr(identity, "sha256", None)
    source = (
        {"sha256": sha256}
        if sha256
        else {
            "archive_id": getattr(item, "archive_id", None),
            "library_file_id": getattr(item, "library_file_id", None),
            "queue_source_id": getattr(item, "queue_source_id", None),
            "revision": identity.revision() if identity else None,
        }
    )
    return {"source": source, "plate": getattr(item, "plate_id", None)}


def offline_params(requirements, policy, snapshot, missing, feed):
    params = routing_params(
        "printer_off_missing_filament", requirements, policy, snapshot, tuple(item["slot"] for item in missing)
    )
    wanted = {}
    for item in params["wanted"]:
        wanted[item["material"]] = wanted.get(item["material"], False) or "color" in item
    params["offline_loaded"] = [
        (
            canonical_filament_type(source.material),
            normalized_color(source.declared_color or source.color)
            if wanted[canonical_filament_type(source.material)]
            else None,
        )
        for source in feed.sources
        if canonical_filament_type(source.material) in wanted
        and (policy.feed_policy == "auto" or source.kind == ("ams" if policy.feed_policy == "ams_only" else "external"))
    ]
    return params


def more_issues(language, count):
    if language == "uk":
        category = (
            "one"
            if count % 10 == 1 and count % 100 != 11
            else "few"
            if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14
            else "many"
        )
    else:
        category = "one" if count == 1 else "many"
    return t(language, "filament_routing", f"autoqueue_more_issues_{category}", count=count)


@dataclass(frozen=True)
class WaitingNotice:
    job_name: str
    target_model: str
    waiting_reason: str
