"""Canonical part names.

Rules measured against the whole farm (2026-08-29 survey: 892 sliced 3MFs,
1579 plates, 13 463 instances): the extension anchor folds the dominant
``X.stl_2`` and ``X.stl 3`` dialects unconditionally, ``(N)`` is the
BambuStudio clone suffix, and extensionless ``base_N`` folds only beside a
sibling on the same plate so genuine names like ``v2_bracket_3`` survive.
"""

import pytest

from backend.app.services.part_names import PartTally, canonicalize, name_key, tally_objects


class TestExtensionAnchor:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("swapmod_FUCS_v00-19_2026-02-18-122236.stl_2", "swapmod_FUCS_v00-19_2026-02-18-122236.stl"),
            ("W02453_DA_kamik_yellow_ultranew.stl 14", "W02453_DA_kamik_yellow_ultranew.stl"),
            ("part.STEP-3", "part.STEP"),
            ("model.3mf_1", "model.3mf"),
        ],
    )
    def test_a_copy_counter_after_a_model_extension_always_folds(self, raw, expected):
        assert canonicalize(raw) == expected

    def test_a_bare_extension_name_is_untouched(self):
        assert canonicalize("HFB1200-1.STL") == "HFB1200-1.STL"


class TestParenClone:
    def test_bambustudio_clone_suffix_folds(self):
        assert canonicalize("Netzkörper1 (1)") == "Netzkörper1"

    def test_a_pure_paren_name_is_not_emptied(self):
        assert canonicalize("(2)") == "(2)"


class TestSiblingRule:
    def test_extensionless_base_n_folds_beside_its_sibling(self):
        plate = ["bracket_1", "bracket_2"]
        assert canonicalize("bracket_1", plate) == "bracket"

    def test_the_bare_base_also_counts_as_a_sibling(self):
        plate = ["bracket", "bracket_2"]
        assert canonicalize("bracket_2", plate) == "bracket"

    def test_a_lone_numeric_tail_is_a_genuine_name(self):
        plate = ["v2_bracket_3", "lid"]
        assert canonicalize("v2_bracket_3", plate) == "v2_bracket_3"

    def test_no_plate_context_means_no_sibling_folding(self):
        assert canonicalize("bracket_2") == "bracket_2"


class TestNameKey:
    def test_key_is_case_insensitive(self):
        assert name_key(canonicalize("HFB1200-1.STL")) == name_key(canonicalize("hfb1200-1.stl"))

    def test_whitespace_is_collapsed_before_matching(self):
        assert canonicalize("  lid   left ") == "lid left"


class TestTallyObjects:
    def test_instances_group_by_canonical_name_with_their_ids(self):
        objects = {941: "part.stl_1", 942: "part.stl_2", 943: "lid"}
        tallies = {t.name_key: t for t in tally_objects(objects)}
        assert tallies["part.stl"].quantity == 2
        assert sorted(tallies["part.stl"].identify_ids) == [941, 942]
        assert tallies["lid"].quantity == 1

    def test_case_variants_share_a_row_and_keep_one_spelling(self):
        objects = {1: "HFB1200-1.STL", 2: "hfb1200-1.stl"}
        tallies = tally_objects(objects)
        assert len(tallies) == 1
        assert tallies[0].quantity == 2

    def test_empty_input_gives_empty_output(self):
        assert tally_objects({}) == []

    def test_the_unnamed_meshid_family_collapses_via_siblings(self):
        objects = {i: f"Object_{57714 + i * 44}" for i in range(5)}
        tallies = tally_objects(objects)
        assert len(tallies) == 1
        assert tallies[0].name == "Object"
        assert tallies[0].quantity == 5
