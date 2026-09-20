"""Routing checks complete assignments, colour policy and physical topology."""

from dataclasses import replace

import pytest

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
    req = requirements({"tray_info_idx": "GFA01"})
    assert resolve_filament_routing(req, RoutingPolicy(), snapshot(feed(variant="GFA00"))).reason == "variant_mismatch"
    assert resolve_filament_routing(req, RoutingPolicy(), snapshot(feed())).status == "compatible"


def test_family_material_match_uses_filament_type_not_a_profile_name():
    req = requirements({"type": "333Print PETG", "filament_type": "PETG", "tray_info_idx": "P333PETG"})
    state = snapshot(feed(material="PETG", variant="GFG99"))
    assert resolve_filament_routing(req, RoutingPolicy(), state).status == "compatible"
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
