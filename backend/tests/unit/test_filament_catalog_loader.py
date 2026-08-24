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


class TestSettingIdCollisions:
    """Bambu reused the GFSR99 setting-id space: the bare ``GFSR99`` (and
    ``GFSR99_10``) is the legacy Generic TPU (family GFU99), while the other
    ``GFSR99_NN`` variants are Generic EVA (GFR99). The index once let an EVA
    variant's BASE alias clobber the exact bare TPU key — a tray configured
    as Generic TPU in Bambu Studio (tray_info_idx=GFSR99) then resolved to
    Generic EVA everywhere (measured live 2026-08-25: three TPU spools linked
    to GFR99). An exact setting id must never lose to a stripped alias."""

    def test_bare_gfsr99_is_generic_tpu(self):
        from backend.app.utils import filament_catalog as cat

        preset = cat.preset_for_setting_id("GFSR99")
        assert preset is not None
        assert preset.filament_id == "GFU99"
        assert preset.name == "Generic TPU"

    def test_versioned_eva_variants_still_resolve_to_eva(self):
        from backend.app.utils import filament_catalog as cat

        preset = cat.preset_for_setting_id("GFSR99_01")
        assert preset is not None
        assert preset.filament_id == "GFR99"

    def test_the_tpu_versioned_exception_keeps_its_family(self):
        from backend.app.utils import filament_catalog as cat

        preset = cat.preset_for_setting_id("GFSR99_10")
        assert preset is not None
        assert preset.filament_id == "GFU99"
