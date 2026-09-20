"""Routing checks complete assignments, colour policy and physical topology."""

from dataclasses import replace

import pytest

from backend.app.services.filament_policy import choices_policy
from backend.app.services.filament_requirements import PrintRequirements, SourceIdentity
from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
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


def test_pinned_sources_are_not_remapped_and_unknown_legacy_colour_needs_review():
    req = requirements({})
    policy = RoutingPolicy(mode="pinned", physical_pins={1: {"source_id": 0}})
    state = snapshot(feed(0, "00FF00", kind="ams"), feed(1, kind="ams"))
    assert resolve_filament_routing(req, policy, state).reason == "mapping_review_required"
    explicit = replace(policy, physical_pins={1: {"source_id": 0, "color": "00FF00", "type": "PLA"}})
    assert resolve_filament_routing(req, explicit, state).plan.mapping == [0]


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
