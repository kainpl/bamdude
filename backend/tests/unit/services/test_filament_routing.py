"""Routing checks complete assignments, colour policy and physical topology."""

import inspect
from dataclasses import replace

import pytest

from backend.app.services.filament_policy import choices_policy
from backend.app.services.filament_preflight import DispatchRoutingGuard, feed_signature
from backend.app.services.filament_requirements import PrintRequirements, SourceIdentity
from backend.app.services.filament_routing import RoutingDeferred, RoutingPolicy, resolve_filament_routing
from backend.app.services.printer_feed_snapshot import FeedSource, PrinterFeedSnapshot
from backend.app.utils.printer_models import DUAL_NOZZLE_MODELS, normalize_model_name


def requirements(*slots, model="P1P"):
    return PrintRequirements(
        "ok",
        source_identity=SourceIdentity("synthetic", 1, 1),
        resolved_plate_id=4,
        model=model,
        used_filaments=tuple(
            {
                "slot_id": i + 1,
                "type": "PLA",
                "color": "#FF0000",
                "nozzle_id": 0,
                "used_grams": 1,
                "tray_info_idx": None,
                **s,
            }
            for i, s in enumerate(slots)
        ),
    )


def snapshot(*sources, model="P1P", **kwargs):
    return PrinterFeedSnapshot(
        1, model, True, 1, "revision", True, any(s.kind == "ams" for s in sources), True, tuple(sources), **kwargs
    )


def feed(sid=254, color="FF0000FF", *, kind="external", material="PLA", nozzle=0, **kwargs):
    return FeedSource(sid, kind, material, color, nozzles=(nozzle,), **kwargs)


def test_sparse_external_serializes_padding_without_enabling_ams():
    result = resolve_filament_routing(requirements({"slot_id": 4}), RoutingPolicy(), snapshot(feed()))
    assert result.status == "compatible"
    assert result.plan.mapping == [-1, -1, -1, 254]
    assert result.plan.use_ams is False


def test_reconnecting_after_fts_does_not_authorize_an_explicit_external_path():
    result = resolve_filament_routing(
        requirements({"slot_id": 4}), RoutingPolicy(), snapshot(feed(), fts_pending_confirmation=True)
    )
    assert result.status == "unknown"
    assert result.reason == "fts_state_unavailable"


def test_global_strict_without_overrides_and_relaxed_zero_colour_matches():
    req, state = requirements({}), snapshot(feed(color="00FF00"))
    assert resolve_filament_routing(req, RoutingPolicy(force_color_match=True), state).reason == "color_mismatch"
    assert resolve_filament_routing(req, RoutingPolicy(), state).status == "compatible"


def test_flexible_channel_cannot_take_only_source_of_strict_channel():
    req = requirements({}, {"color": "#00FF00"})
    policy = RoutingPolicy(filament_overrides=({"slot_id": 2, "force_color_match": True},))
    state = snapshot(feed(0, "00FF00", kind="ams"), feed(1, "0000FF", kind="ams"))
    result = resolve_filament_routing(req, policy, state)
    assert result.status == "compatible"
    assert result.plan.mapping == [1, 0]


@pytest.mark.parametrize("strict", [True, False])
def test_single_external_cannot_satisfy_two_inputs(strict):
    result = resolve_filament_routing(requirements({}, {}), RoutingPolicy(force_color_match=strict), snapshot(feed()))
    assert result.status == "incompatible"


def test_single_nozzle_does_not_mix_ams_and_external():
    result = resolve_filament_routing(requirements({}, {}), RoutingPolicy(), snapshot(feed(), feed(0, kind="ams")))
    assert result.status == "incompatible"


@pytest.mark.parametrize("incomplete,expected", [(False, "incompatible"), (True, "unknown")])
def test_petg_profile_refusal_retains_uncertainty_when_feed_is_incomplete(incomplete, expected):
    """A partial reason is not a complete verdict: the dialog must show both."""
    req = requirements({"type": "PETG", "tray_info_idx": "P8e36324"}, model="P1S")
    state = snapshot(feed(0, kind="ams", material="PETG", variant="GFG99"), model="P1S", incomplete=incomplete)
    result = resolve_filament_routing(req, RoutingPolicy(allow_base_material_match=False), state)
    assert result.status == expected
    assert result.reason == "variant_mismatch"
    assert result.plan is None


@pytest.mark.parametrize("model", sorted(DUAL_NOZZLE_MODELS))
@pytest.mark.parametrize("reverse", [True, False])
def test_all_dual_models_keep_mixed_bindings(model, reverse):
    model = normalize_model_name(model)
    left, right = (0, 1) if reverse else (1, 0)
    req = requirements({"nozzle_id": left}, {"nozzle_id": right}, model=model)
    state = snapshot(feed(0, kind="ams", nozzle=left), feed(255 - right, nozzle=right), model=model)
    result = resolve_filament_routing(req, RoutingPolicy(), state)
    assert result.status == "compatible"
    assert result.plan.mapping == [0, 255 - right]
    assert result.plan.use_ams is True


def test_dual_external_without_ams():
    req = requirements({"nozzle_id": 1}, {"nozzle_id": 0}, model="X2D")
    result = resolve_filament_routing(
        req, RoutingPolicy(), snapshot(feed(254, nozzle=1), feed(255, nozzle=0), model="X2D")
    )
    assert result.status == "compatible"
    assert result.plan.mapping == [254, 255]
    assert result.plan.use_ams is False


def test_known_variant_mismatch_is_not_relaxed_by_colour_policy():
    """Colour policy has no say over the profile gate; the base-material option does.

    That option is pinned off here because it is the only thing that arms the
    gate. With it ON — the default — GFA01 routes onto GFA00: both report
    ``tray_type == "PLA"``, and «any PLA will do» is precisely what the option
    says. This has held for every id the catalogue can name since the option
    landed (a resolvable family already disarmed the gate); the fixture below
    omits ``filament_type``, so it used to reach the gate by accident and read
    as if the default still enforced profiles.
    """
    req = requirements({"tray_info_idx": "GFA01"})
    strict = RoutingPolicy(allow_base_material_match=False)
    assert resolve_filament_routing(req, strict, snapshot(feed(variant="GFA00"))).reason == "variant_mismatch"
    assert resolve_filament_routing(req, strict, snapshot(feed())).status == "compatible"
    assert (
        resolve_filament_routing(req, replace(strict, force_color_match=True), snapshot(feed(variant="GFA00"))).reason
        == "variant_mismatch"
    )


def test_a_profile_the_catalogue_cannot_name_still_prints_on_its_base_material():
    """ABS prints on ABS, whatever id either side carries.

    A custom slicer preset ("Pa240002") resolves to no family in the catalogue
    at all — and that used to switch the option OFF and re-arm the id
    comparison it exists to suppress. Measured on a 24-printer farm
    (2026-09-20): every machine with ABS in the AMS refused an ABS plate,
    blaming the filament type. The sibling tests below hold the other half of
    the rule: a family that DOES resolve is not consulted either.
    """
    req = requirements({"type": "ABS", "tray_info_idx": "Pa240002"})
    state = snapshot(feed(material="ABS", variant="GFB99"))
    assert resolve_filament_routing(req, RoutingPolicy(), state).status == "compatible"
    # Off is still off: the operator asked for that exact profile.
    assert (
        resolve_filament_routing(req, RoutingPolicy(allow_base_material_match=False), state).reason
        == "variant_mismatch"
    )


def test_a_channel_is_matched_on_its_own_material_not_on_the_catalogue_family():
    """The file's declared material decides; a family that disagrees does not overrule it.

    ``filament_type`` is whatever the catalogue resolves this channel's
    ``tray_info_idx`` to, and the two can disagree — a preset re-pointed in the
    cloud, a stale row, an id another vendor reused. The plate will extrude what
    the slicer sliced it for, so that is the material compared.
    """
    req = requirements({"type": "ABS", "filament_type": "PLA", "tray_info_idx": "GFB00"})
    assert resolve_filament_routing(req, RoutingPolicy(), snapshot(feed(material="ABS"))).status == "compatible"
    assert resolve_filament_routing(req, RoutingPolicy(), snapshot(feed(material="PLA"))).reason == "material_mismatch"


@pytest.mark.parametrize("material", ["PVB", "PC-ABS"])
def test_a_material_the_bundled_catalogue_never_names_still_matches_itself(material):
    """There is no whitelist of relaxable materials, and there must never be one.

    The option says «the base material on both sides», not «one of the materials
    we happened to think of». A farm printing PVB or PC-ABS is entitled to it on
    the same terms as one printing PLA.
    """
    req = requirements({"type": material, "tray_info_idx": "Pxxx"})
    assert (
        resolve_filament_routing(req, RoutingPolicy(), snapshot(feed(material=material, variant="GFZ00"))).status
        == "compatible"
    )


def test_equivalent_materials_are_canonicalised_before_they_are_compared():
    """PA12-CF and PA-CF are one material to the printer, and the gate agrees.

    ``filament_types_compatible`` already folds the group; pinned here because
    the gate now has nothing else left to relax a name with.
    """
    req = requirements({"type": "PA12-CF"})
    assert resolve_filament_routing(req, RoutingPolicy(), snapshot(feed(material="PA-CF"))).status == "compatible"


def test_a_profile_name_written_in_the_material_field_is_not_repaired_by_the_catalogue():
    """Deferred decision Д1b — chosen, not forgotten.

    Some slicer presets write their own NAME where the structured material
    belongs ("333Print PETG"), and the catalogue used to paper over that by
    substituting the family the preset id resolves to. That substitution is
    gone: the resolver compares the file's own declared material, so such a
    plate matches no PETG tray — with the option on or off. Reviving it means
    repairing the type where the file is READ, not deciding routing from an id
    the catalogue may or may not know; nothing guarantees the old behaviour in
    the meantime.
    """
    req = requirements({"type": "333Print PETG", "filament_type": "PETG", "tray_info_idx": "P333PETG"})
    state = snapshot(feed(material="PETG", variant="GFG99"))
    assert resolve_filament_routing(req, RoutingPolicy(), state).reason == "material_mismatch"
    assert (
        resolve_filament_routing(req, RoutingPolicy(allow_base_material_match=False), state).reason
        == "material_mismatch"
    )


def test_explicit_external_only_works_even_with_ams():
    result = resolve_filament_routing(
        requirements({}), RoutingPolicy(feed_policy="external_only"), snapshot(feed(), feed(0, kind="ams"))
    )
    assert result.plan.mapping == [254]


def test_a_legacy_pin_without_a_colour_holds_unless_the_colour_is_forced():
    """П4 (owner, 2026-09-30): a pin's colour counts only when the colour is forced."""
    req = requirements({})
    policy = RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": 0}})
    state = snapshot(feed(0, "00FF00", kind="ams"), feed(1, kind="ams"))
    assert resolve_filament_routing(req, policy, state).plan.mapping == [0]
    assert resolve_filament_routing(req, replace(policy, force_color_match=True), state).reason == "color_mismatch"


def masked(sid, *, color, declared_color, variant="GFG00", declared_variant="GFG99", material="PETG", remain=-1):
    return FeedSource(
        sid,
        "ams",
        material,
        color,
        variant,
        (0,),
        remain,
        declared_color=declared_color,
        declared_variant=declared_variant,
    )


def test_a_forced_colour_is_judged_by_the_colour_the_operator_declared():
    state = snapshot(masked(0, color="FF0000FF", declared_color="000000FF"))
    forced = RoutingPolicy(force_color_match=True)
    black = requirements({"type": "PETG", "color": "#000000"})
    red = requirements({"type": "PETG", "color": "#FF0000"})
    assert resolve_filament_routing(black, forced, state).plan.mapping == [0]
    assert resolve_filament_routing(red, forced, state).reason == "color_mismatch"


def test_a_strict_profile_is_judged_by_the_profile_the_operator_declared():
    state = snapshot(masked(0, color="000000FF", declared_color="000000FF", variant="GFG02", declared_variant="GFG99"))
    strict = RoutingPolicy(allow_base_material_match=False)
    generic = requirements({"type": "PETG", "color": "#000000", "tray_info_idx": "GFG99"})
    hf = requirements({"type": "PETG", "color": "#000000", "tray_info_idx": "GFG02"})
    assert resolve_filament_routing(generic, strict, state).status == "compatible"
    result = resolve_filament_routing(hf, strict, state)
    assert result.reason == "variant_mismatch"
    assert result.params["loaded"] == "PETG (GFG99)"  # the refusal names what the rule read


def test_the_base_material_is_always_the_spools():
    state = snapshot(masked(0, color="000000FF", declared_color="000000FF"))
    assert (
        resolve_filament_routing(requirements({"type": "PLA", "color": "#000000"}), RoutingPolicy(), state).plan is None
    )


def test_a_pin_checks_colour_only_when_the_colour_is_forced():
    pinned = RoutingPolicy(
        mode="pinned", physical_pins={1: {"source_id": 0, "type": "PLA", "color": "00FF00FF", "nozzles": [0]}}
    )
    state = snapshot(feed(0, "0000FFFF", kind="ams"))
    assert resolve_filament_routing(requirements({}), pinned, state).plan.mapping == [0]
    assert (
        resolve_filament_routing(requirements({}), replace(pinned, force_color_match=True), state).reason
        == "color_mismatch"
    )


PIN_0 = RoutingPolicy(
    mode="pinned", physical_pins={1: {"source_id": 0, "type": "PETG", "color": "000000FF", "nozzles": [0]}}
)
PETG_BLACK = requirements({"type": "PETG", "color": "#000000"})


def twin_state(*, backup=True, membership=None):
    return snapshot(
        FeedSource(1, "ams", "PETG", "FF0000FF", nozzles=(0,), declared_color="000000FF"),
        FeedSource(2, "ams", "PLA", "000000FF", nozzles=(0,)),
        backup_enabled=backup,
        backup_membership={0: (0, 1), 1: (0, 1)} if membership is None else membership,
    )


def test_an_empty_pinned_slot_prints_from_its_backup_twin():
    assert resolve_filament_routing(PETG_BLACK, PIN_0, twin_state()).plan.mapping == [1]


def test_a_twin_still_meets_a_forced_colour_by_what_was_declared():
    forced = replace(PIN_0, force_color_match=True)
    assert resolve_filament_routing(PETG_BLACK, forced, twin_state()).plan.mapping == [1]
    red = requirements({"type": "PETG", "color": "#FF0000"})
    # The chosen slot is empty and no twin can stand in: that is what the operator is told.
    assert resolve_filament_routing(red, forced, twin_state()).reason == "pinned_source_empty"


@pytest.mark.parametrize("backup", [False, None])
def test_without_backup_an_empty_pinned_slot_waits(backup):
    assert resolve_filament_routing(PETG_BLACK, PIN_0, twin_state(backup=backup)).reason == "pinned_source_empty"


def test_a_slot_never_seen_in_a_group_has_no_twin():
    """Also the state after a BamDude restart: the memory is empty."""
    assert resolve_filament_routing(PETG_BLACK, PIN_0, twin_state(membership={})).reason == "pinned_source_empty"


def test_a_twin_on_another_nozzle_is_not_used():
    state = snapshot(
        FeedSource(1, "ams", "PETG", "000000FF", nozzles=(1,)), backup_enabled=True, backup_membership={0: (0, 1)}
    )
    result = resolve_filament_routing(PETG_BLACK, PIN_0, state)
    assert result.plan is None and result.reason == "pinned_source_empty"


@pytest.mark.parametrize("fts", [{"fts": True}, {"fts_pending_confirmation": True}])
def test_an_empty_pinned_slot_is_not_blamed_on_another_holder(fts):
    """Final review: an external spool on an FTS printer used to lend its FTS
    reason to an empty pinned AMS slot."""
    state = snapshot(feed(254, "000000FF", material="PETG"), backup_enabled=True, **fts)
    assert resolve_filament_routing(PETG_BLACK, PIN_0, state).reason == "pinned_source_empty"


def test_a_loaded_pinned_slot_is_never_swapped_for_a_twin():
    state = snapshot(
        FeedSource(0, "ams", "PLA", "000000FF", nozzles=(0,)),
        FeedSource(1, "ams", "PETG", "000000FF", nozzles=(0,)),
        backup_enabled=True,
        backup_membership={0: (0, 1), 1: (0, 1)},
    )
    result = resolve_filament_routing(PETG_BLACK, PIN_0, state)
    assert result.plan is None and result.reason == "material_mismatch"


def test_an_external_holder_is_never_a_twin():
    state = snapshot(feed(254, "000000FF", material="PETG"), backup_enabled=True, backup_membership={0: (0, 254)})
    assert resolve_filament_routing(PETG_BLACK, PIN_0, state).reason == "pinned_source_empty"


def test_an_ams_ht_slot_has_twins_too():
    pin = RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": 128, "type": "PETG", "nozzles": [0]}})
    state = snapshot(
        FeedSource(129, "ams", "PETG", "000000FF", nozzles=(0,)),
        backup_enabled=True,
        backup_membership={128: (128, 129)},
    )
    assert resolve_filament_routing(PETG_BLACK, pin, state).plan.mapping == [129]


def test_a_legacy_row_gets_no_twin():
    result = resolve_filament_routing(PETG_BLACK, PIN_0, twin_state(), allow_backup_twins=False)
    assert result.reason == "pinned_source_empty"


def test_a_twin_already_serving_another_channel_is_not_taken_twice():
    req = requirements({"type": "PETG", "color": "#000000"}, {"type": "PETG", "color": "#000000"})
    policy = RoutingPolicy(
        mode="pinned",
        physical_pins={
            1: {"source_id": 0, "type": "PETG", "nozzles": [0]},
            2: {"source_id": 1, "type": "PETG", "nozzles": [0]},
        },
    )
    state = snapshot(
        FeedSource(1, "ams", "PETG", "000000FF", nozzles=(0,)),
        backup_enabled=True,
        backup_membership={0: (0, 1), 1: (0, 1)},
    )
    assert resolve_filament_routing(req, policy, state).plan is None


def test_the_ranking_counts_a_declared_colour_as_exact():
    """В1: in a leftover group declared black, «lowest remain first» starts on the leftovers."""
    state = snapshot(
        FeedSource(0, "ams", "PETG", "000000FF", nozzles=(0,), remain=90),
        FeedSource(1, "ams", "PETG", "FF0000FF", nozzles=(0,), remain=10, declared_color="000000FF"),
    )
    req = requirements({"type": "PETG", "color": "#000000"})
    assert resolve_filament_routing(req, RoutingPolicy(), state, prefer_lowest=True).plan.mapping == [1]


def test_a_pinned_tray_that_was_only_re_profiled_is_still_the_pinned_tray():
    """A pin is a physical slot, and the base-material option governs its profile clause too.

    The pin is built the way production builds it — ``choices_policy`` records
    what the chosen source WAS at pin time, ``tray_info_idx`` included. Re-tagging
    that spool's profile afterwards (GFB99 → GFB00) moved no filament, so with
    «allow base material match» on the tray is still the one the operator pointed
    at. With the option off the operator asked for that exact profile, here as in
    the unpinned gate above.
    """
    pinned = snapshot(feed(0, "000000FF", kind="ams", material="ABS", variant="GFB99"))
    policy = choices_policy({"ams_mapping": [0], "manual_mapping": True}, pinned)
    req = requirements({"type": "ABS", "color": "#000000"})
    reprofiled = snapshot(feed(0, "000000FF", kind="ams", material="ABS", variant="GFB00"))

    result = resolve_filament_routing(req, policy, reprofiled)
    assert result.status == "compatible"
    assert result.plan.mapping == [0]
    assert (
        resolve_filament_routing(req, replace(policy, allow_base_material_match=False), reprofiled).reason
        == "mapping_review_required"
    )


def test_a_pin_still_catches_a_real_swap_with_base_material_match_on():
    """Only the profile clause is policy-dependent; the physical identity is not.

    The plate here would take the PLA now sitting in the slot, so nothing but the
    pin can notice that it is no longer the ABS spool the operator chose.
    """
    pinned = snapshot(feed(0, "000000FF", kind="ams", material="ABS", variant="GFB99"))
    policy = choices_policy({"ams_mapping": [0], "manual_mapping": True}, pinned)
    swapped = snapshot(feed(0, "000000FF", kind="ams", material="PLA", variant="GFB00"))
    req = requirements({"type": "PLA", "color": "#000000"})
    assert resolve_filament_routing(req, policy, swapped).reason == "mapping_review_required"


def test_prefer_lowest_is_gated_by_backup_and_secondary_to_exact_colour():
    req = requirements({})
    state = snapshot(feed(0, kind="ams", remain=80), feed(1, kind="ams", remain=20), backup_enabled=True)
    assert resolve_filament_routing(req, RoutingPolicy(), state, prefer_lowest=True).plan.mapping == [1]
    assert resolve_filament_routing(
        req, RoutingPolicy(), replace(state, backup_enabled=False), prefer_lowest=True
    ).plan.mapping == [0]


def test_nozzle_diameter_uses_physical_map_and_unknown_is_not_a_match():
    req = replace(
        requirements({"nozzle_id": 1}, model="H2D"),
        nozzle_constraints={"physical_extruder_map": ["1", "0"], "nozzle_diameter": ["0.6", "0.4"]},
    )
    state = snapshot(feed(0, kind="ams", nozzle=1), model="H2D")
    assert resolve_filament_routing(req, RoutingPolicy(), state).reason == "nozzle_state_unavailable"
    assert (
        resolve_filament_routing(req, RoutingPolicy(), replace(state, nozzle_diameters={1: (0.4,)})).reason
        == "nozzle_mismatch"
    )
    assert (
        resolve_filament_routing(req, RoutingPolicy(), replace(state, nozzle_diameters={1: (0.6,)})).status
        == "compatible"
    )


def test_no_channel_merge_even_for_same_colour():
    state = snapshot(feed(0, kind="ams"))
    assert resolve_filament_routing(requirements({}, {}), RoutingPolicy(), state).reason == "distinct_sources_required"


def test_fts_source_reaches_either_extruder():
    req = requirements({"nozzle_id": 1}, model="X2D")
    state = snapshot(replace(feed(0, kind="ams"), nozzles=(0, 1)), model="X2D", fts=True)
    assert resolve_filament_routing(req, RoutingPolicy(), state).status == "compatible"


@pytest.mark.parametrize("policy", [RoutingPolicy(), RoutingPolicy(feed_policy="external_only")])
def test_fts_never_routes_an_external_source(policy):
    req = requirements({"nozzle_id": 1}, model="H2D")
    state = snapshot(feed(254, nozzle=1), model="H2D", fts=True)
    result = resolve_filament_routing(req, policy, state)
    assert result.status == "incompatible"
    assert result.reason == "fts_external_unsupported"


def test_left_tpu_requires_the_applicable_firmware_capability_but_right_does_not():
    left = requirements({"type": "TPU", "nozzle_id": 1}, model="H2D")
    right = requirements({"type": "TPU", "nozzle_id": 0}, model="H2D")
    left_source = feed(0, kind="ams", material="TPU", nozzle=1)
    right_source = feed(1, kind="ams", material="TPU", nozzle=0)

    unknown = resolve_filament_routing(left, RoutingPolicy(), snapshot(left_source, model="H2D"))
    assert unknown.status == "unknown" and unknown.reason == "tpu_left_firmware_unavailable"
    refused = resolve_filament_routing(
        left, RoutingPolicy(), snapshot(left_source, model="H2D", left_tpu_firmware=False)
    )
    assert refused.status == "incompatible" and refused.reason == "tpu_left_firmware_unsupported"
    assert (
        resolve_filament_routing(
            left, RoutingPolicy(), snapshot(left_source, model="H2D", left_tpu_firmware=True)
        ).status
        == "compatible"
    )
    assert (
        resolve_filament_routing(
            right, RoutingPolicy(), snapshot(right_source, model="H2D", left_tpu_firmware=False)
        ).status
        == "compatible"
    )


# --------------------------------------------------------------------------- #
# The dispatch boundary reads a plan under the policy it was made with
# --------------------------------------------------------------------------- #


def a_plan(policy, state, req=None):
    result = resolve_filament_routing(req or requirements({}), policy, state)
    assert result.plan is not None, result.reason
    return result.plan


def a_guard(policy, state, req=None):
    from backend.app.services.filament_preflight import planned_nozzle_diameters
    from backend.app.utils.printer_configs import requires_left_tpu_firmware_check

    req = req or requirements({})
    plan = a_plan(policy, state, req)
    return DispatchRoutingGuard(
        req,
        policy,
        plan,
        True,
        "revision",
        state.generation,
        planned_nozzle_diameters(req, policy, plan, state),
        requires_left_tpu_firmware_check(state.model),
    )


def validate(guard, state):
    guard.validate(state, mapping=guard.plan.mapping, use_ams=guard.plan.use_ams, plate_id=guard.plan.resolved_plate_id)


def retagged(state, source, variant="GFB99"):
    """The one change this feature is about: a new profile id on the same spool."""
    return replace(state, sources=(replace(source, variant=variant),), revision="the raw revision moved")


def test_a_changed_material_never_reaches_the_plan_comparison_at_all():
    """The material is not absent from that list: it is refused before a plan exists."""
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    moved = replace(snapshot(source), sources=(replace(source, material="PETG"),))
    result = resolve_filament_routing(requirements({}), RoutingPolicy(allow_base_material_match=True), moved)
    assert (result.plan, result.reason) == (None, "material_mismatch")


def test_the_feed_signature_of_an_off_job_IS_the_snapshots_own_marker():
    """Byte-for-byte, not merely equivalent — and two things rest on it.

    A job that keeps the profile is compared exactly as it was before the
    boundary learned about policy, so nothing re-hashes and nothing can drift;
    and a ``blocked_revision`` recorded by an older build, which hashed the raw
    marker, still matches such a job and keeps latching it.
    """
    off = RoutingPolicy(allow_base_material_match=False)
    state = snapshot(feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL"))
    assert feed_signature(off, state) == state.marker == (state.generation, state.revision)
    retag = retagged(state, state.sources[0])
    assert feed_signature(off, retag) == retag.marker, "still the raw marker once the feed has moved"


def test_the_feed_signature_ignores_the_profile_only_when_the_option_is_on():
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    state = snapshot(source)
    on, off = RoutingPolicy(allow_base_material_match=True), RoutingPolicy(allow_base_material_match=False)
    assert feed_signature(on, state) == feed_signature(on, retagged(state, source))
    assert feed_signature(off, state) != feed_signature(off, retagged(state, source))


@pytest.mark.parametrize(
    "changed",
    [
        {"material": "PETG"},
        {"color": "00FF00FF"},
        {"identity": "ANOTHER-SPOOL"},
        {"nozzles": (0, 1)},
        {"kind": "external"},
        {"id": 1},
    ],
)
def test_the_feed_signature_keeps_every_physical_fact_with_the_option_on(changed):
    policy = RoutingPolicy(allow_base_material_match=True)
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    state = snapshot(source)
    assert feed_signature(policy, state) != feed_signature(
        policy, replace(state, sources=(replace(source, **changed),))
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"generation": 2},
        {"nozzle_diameters": {0: (0.4,)}},
        {"ams_known": False},
        {"ams_present": False},
        {"external_known": False},
        {"incomplete": True},
        {"fts": True},
        {"backup_enabled": False},
        {"model": "P1S"},
        {"sources": ()},
    ],
)
def test_the_feed_signature_keeps_the_connection_and_the_shape_of_the_feed(changed):
    policy = RoutingPolicy(allow_base_material_match=True)
    state = snapshot(feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL"))
    assert feed_signature(policy, state) != feed_signature(policy, replace(state, **changed))


def test_re_advertising_the_generic_family_does_not_move_the_on_signature():
    policy = RoutingPolicy(allow_base_material_match=True)
    source = feed(0, kind="ams", variant="GFG00", declared_variant="GFG99")
    moved = replace(source, declared_variant="GFG98")
    assert feed_signature(policy, snapshot(source)) == feed_signature(policy, snapshot(moved))


def test_the_guard_runs_without_an_await_and_lets_a_retag_through():
    """``validate`` is called inside the MQTT client's routing lock: no await, ever."""
    assert not inspect.iscoroutinefunction(DispatchRoutingGuard.validate)
    policy = RoutingPolicy(allow_base_material_match=True)
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    guard = a_guard(policy, snapshot(source))
    assert (
        guard.validate(
            retagged(snapshot(source), source),
            mapping=guard.plan.mapping,
            use_ams=guard.plan.use_ams,
            plate_id=guard.plan.resolved_plate_id,
        )
        is None
    )


@pytest.mark.parametrize("changed", [{"color": "00FF00FF"}, {"identity": "ANOTHER-SPOOL"}, {"variant": "GFB99"}])
def test_the_guard_lets_the_same_filament_through_whatever_its_tag_or_colour(changed):
    policy = RoutingPolicy(allow_base_material_match=True)
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    guard = a_guard(policy, snapshot(source))
    validate(guard, replace(snapshot(source), sources=(replace(source, **changed),)))


@pytest.mark.parametrize(
    ("changed", "reason"), [({"material": "PETG"}, "material_mismatch"), ({"nozzles": (1,)}, "nozzle_mismatch")]
)
def test_the_guard_refuses_a_spool_that_no_longer_fits_its_channel(changed, reason):
    policy = RoutingPolicy(allow_base_material_match=True)
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    guard = a_guard(policy, snapshot(source))
    with pytest.raises(RoutingDeferred, match=reason):
        validate(guard, replace(snapshot(source), sources=(replace(source, **changed),)))


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"generation": 2}, "printer_reconnected"),
        ({"connected": False}, "printer_offline"),
        ({"sources": ()}, "planned_source_empty"),
    ],
)
def test_the_guard_still_refuses_a_lost_connection_or_an_empty_feed(changed, reason):
    policy = RoutingPolicy(allow_base_material_match=True)
    guard = a_guard(policy, snapshot(feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")))
    state = replace(snapshot(feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")), **changed)
    with pytest.raises(RoutingDeferred, match=reason):
        validate(guard, state)


def test_the_guard_refuses_a_profile_retag_only_when_the_job_is_strict_about_it():
    source = feed(0, kind="ams", variant="GFA00", identity="THE-SPOOL")
    strict = RoutingPolicy(allow_base_material_match=False)
    named = requirements({"tray_info_idx": "GFA00"})
    guard = a_guard(strict, snapshot(source), named)
    with pytest.raises(RoutingDeferred, match="variant_mismatch"):
        validate(guard, retagged(snapshot(source), source))
    validate(a_guard(strict, snapshot(source)), retagged(snapshot(source), source))  # the file names no profile


def test_a_change_in_a_slot_the_plan_does_not_use_is_not_a_changed_plan():
    policy = RoutingPolicy()
    used, spare = feed(0, kind="ams"), feed(1, "00FF00FF", kind="ams", material="PETG")
    guard = a_guard(policy, snapshot(used, spare))
    assert guard.plan.mapping == [0]
    validate(guard, snapshot(used, replace(spare, material="ABS", identity="NEW")))
    validate(guard, snapshot(used))  # and a spare slot emptied


def test_the_guard_reads_the_declared_colour_for_a_forced_colour():
    forced = RoutingPolicy(force_color_match=True)
    black = requirements({"type": "PETG", "color": "#000000"})
    source = FeedSource(0, "ams", "PETG", "000000FF", nozzles=(0,))
    guard = a_guard(forced, snapshot(source), black)
    validate(guard, snapshot(replace(source, color="FF0000FF", declared_color="000000FF")))
    with pytest.raises(RoutingDeferred, match="color_mismatch"):
        validate(guard, snapshot(replace(source, color="FF0000FF")))


def test_the_guard_refuses_a_nozzle_that_no_longer_fits_the_plate():
    req = PrintRequirements(
        "ok",
        source_identity=SourceIdentity("synthetic", 1, 1),
        resolved_plate_id=4,
        model="P1P",
        used_filaments=({"slot_id": 1, "type": "PLA", "color": "#FF0000", "nozzle_id": 0, "used_grams": 1},),
        nozzle_constraints={"nozzle_diameter": [0.2]},
    )
    state = snapshot(feed(0, kind="ams"), nozzle_diameters={0: (0.2,)})
    guard = a_guard(RoutingPolicy(), state, req)
    with pytest.raises(RoutingDeferred, match="nozzle_mismatch"):
        validate(guard, replace(state, nozzle_diameters={0: (0.4,)}))


def test_the_guard_refuses_a_hotend_swapped_on_a_nozzle_the_plan_uses():
    """Final review I1: a plate with no diameter constraint, or an H2C rack whose
    chosen dock was re-fitted during the soak, is still a changed plan."""
    state = snapshot(feed(0, kind="ams"), nozzle_diameters={0: (0.4,)})
    guard = a_guard(RoutingPolicy(), state)
    with pytest.raises(RoutingDeferred, match="nozzle_mismatch"):
        validate(guard, replace(state, nozzle_diameters={0: (0.6,)}))
    validate(guard, replace(state, nozzle_diameters={0: (0.4,), 1: (0.6,)}))  # a nozzle the plan does not use
    with pytest.raises(RoutingDeferred, match="nozzle_state_unavailable"):
        validate(guard, replace(state, nozzle_diameters={}))  # not reported yet: wait, never pass


def test_the_guard_must_be_told_the_left_tpu_answer():
    """A safety check with a fail-open default is one forgotten argument away from off."""
    with pytest.raises(TypeError):
        DispatchRoutingGuard(requirements({}), RoutingPolicy(), None, True, "revision", 1, {})


def test_plan_holds_edge_cases_refuse_in_words():
    from backend.app.services.filament_preflight import plan_holds

    state = snapshot(feed(0, kind="ams"))
    guard = a_guard(RoutingPolicy(), state)
    unused = replace(guard, policy=RoutingPolicy(filament_overrides=({"slot_id": 9},)))
    assert plan_holds(unused, state) == (False, "override_slot_not_used", False)  # nothing to wait for
    unassigned = replace(guard, plan=replace(guard.plan, assignments={}))
    assert plan_holds(unassigned, state) == (False, "mapping_review_required", False)


def test_an_unknown_fact_is_never_a_pass():
    source = feed(254)
    guard = a_guard(RoutingPolicy(), snapshot(source))
    with pytest.raises(RoutingDeferred, match="fts_state_unavailable"):
        validate(guard, snapshot(source, fts_pending_confirmation=True))


def test_routing_separates_support_and_plain_pla_even_with_legacy_channel_types():
    job = requirements({"tray_info_idx": "GFS05"}, {})
    support = feed(1, kind="ams", material="PLA-S", variant="GFS05")
    ordinary = feed(0, kind="ams", material="PLA", variant="GFA00")
    result = resolve_filament_routing(job, RoutingPolicy(), snapshot(ordinary, support))
    assert result.status == "compatible"
    assert result.plan.mapping == [1, 0]
    assert resolve_filament_routing(job, RoutingPolicy(), snapshot(ordinary)).status == "incompatible"
