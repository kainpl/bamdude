"""Loader over the SHIPPED catalog files — they are part of the app, so
testing against them is testing the artifact users get."""

from backend.app.utils import filament_catalog as cat


def test_get_family_prefers_bambu_and_falls_back_to_orca():
    fam = cat.get_family("GFG99")
    assert fam is not None and fam.alias == "Generic PETG"
    assert cat.get_family("NOPE") is None


def test_setting_id_lookup_accepts_versioned_and_base_forms():
    assert cat.family_for_setting_id("GFSG99_00").filament_id == "GFG99"
    assert cat.family_for_setting_id("GFSG99").filament_id == "GFG99"
    preset = cat.preset_for_setting_id("GFSG99_00")
    assert preset.filament_id == "GFG99"
    assert preset.nozzle_temp_min is not None


def test_preset_by_name_resolves_orca_children_walk():
    # The exact name Orca-cloud children put in `inherits`.
    preset = cat.preset_by_name("Generic PETG @BBL A1M", "orca")
    assert preset is not None and preset.filament_id == "GFG99"


def test_presets_for_family_filters_by_family():
    presets = cat.presets_for_family("GFA00")
    assert presets and all(p.filament_id == "GFA00" for p in presets)


def test_search_families_matches_alias_case_insensitively():
    hits = cat.search_families("generic petg")
    assert any(f.filament_id == "GFG99" for f in hits)


def test_generic_family_for_material():
    assert cat.generic_family_for_material("PETG").filament_id == "GFG99"
    assert cat.generic_family_for_material("PLA").filament_id == "GFL99"
    assert cat.generic_family_for_material("UNOBTAINIUM") is None
