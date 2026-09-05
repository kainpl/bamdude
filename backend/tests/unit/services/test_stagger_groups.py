"""The groups a printer heats in, from tags, locations, or both."""

from __future__ import annotations

from backend.app.services.stagger_groups import GLOBAL, StaggerGroupResolver, StaggerSplit, parse_id_list

TAGS = {1: "Фаза 1", 2: "Фаза 2", 3: "Фаза 3"}
LOCS = {10: "Цех A", 11: "Ряд 1", 12: "Полиця", 20: "Цех B"}
PARENTS = {10: None, 11: 10, 12: 11, 20: None}


def _resolver(split, *, tags_by_printer=None, location_by_printer=None):
    return StaggerGroupResolver(
        split,
        tags_by_printer={k: frozenset(v) for k, v in (tags_by_printer or {}).items()},
        tag_names=TAGS,
        location_by_printer=location_by_printer or {},
        parent_by_location=PARENTS,
        location_names=LOCS,
    )


class TestNoSplit:
    def test_everyone_is_in_the_one_global_group(self):
        r = StaggerGroupResolver.global_only()
        assert r.groups_for(7) == {GLOBAL}
        assert r.universe == {GLOBAL}
        assert r.label(GLOBAL) is None
        assert not r.is_wildcard(7)
        assert not r.tags_split and not r.location_split

    def test_an_axis_switched_on_with_nothing_picked_is_still_global(self):
        r = _resolver(StaggerSplit(by_tags=True, tag_ids=frozenset()))
        assert r.groups_for(1) == {GLOBAL}

    def test_picked_ids_that_no_longer_exist_are_dropped(self):
        r = _resolver(StaggerSplit(by_tags=True, tag_ids=frozenset({99})))
        assert r.universe == {GLOBAL}
        assert not r.tags_split


class TestTags:
    split = StaggerSplit(by_tags=True, tag_ids=frozenset({1, 2, 3}))

    def test_a_tagged_printer_is_in_its_own_group(self):
        r = _resolver(self.split, tags_by_printer={5: {2}})
        assert r.groups_for(5) == {(2, None)}
        assert not r.is_wildcard(5)

    def test_an_untagged_printer_is_a_wildcard_in_every_group(self):
        r = _resolver(self.split, tags_by_printer={})
        assert r.groups_for(5) == {(1, None), (2, None), (3, None)}
        assert r.is_wildcard(5)

    def test_two_phase_tags_count_in_both(self):
        r = _resolver(self.split, tags_by_printer={5: {1, 3}})
        assert r.groups_for(5) == {(1, None), (3, None)}

    def test_a_tag_that_is_not_picked_does_not_count(self):
        r = _resolver(StaggerSplit(by_tags=True, tag_ids=frozenset({1, 2})), tags_by_printer={5: {3}})
        assert r.groups_for(5) == {(1, None), (2, None)}  # only picked tags matter; 3 is a plain label

    def test_labels(self):
        r = _resolver(self.split)
        assert r.label((2, None)) == "Фаза 2"
        assert r.universe == {(1, None), (2, None), (3, None)}


class TestLocations:
    split = StaggerSplit(by_location=True, location_ids=frozenset({10, 20}))

    def test_the_location_itself_counts(self):
        r = _resolver(self.split, location_by_printer={5: 10})
        assert r.groups_for(5) == {(None, 10)}

    def test_the_nearest_picked_ancestor_wins(self):
        r = _resolver(StaggerSplit(by_location=True, location_ids=frozenset({10, 11})), location_by_printer={5: 12})
        assert r.groups_for(5) == {(None, 11)}  # Полиця → Ряд 1 (picked) before Цех A (picked)

    def test_a_deep_chain_reaches_the_root(self):
        r = _resolver(self.split, location_by_printer={5: 12})
        assert r.groups_for(5) == {(None, 10)}

    def test_no_picked_ancestor_is_a_wildcard(self):
        r = _resolver(self.split, location_by_printer={5: None, 6: 11})
        r6 = _resolver(StaggerSplit(by_location=True, location_ids=frozenset({20})), location_by_printer={6: 11})
        assert r.groups_for(5) == {(None, 10), (None, 20)}
        assert r.is_wildcard(5)
        assert r6.groups_for(6) == {(None, 20)} and r6.is_wildcard(6)

    def test_a_ring_in_the_tree_terminates(self):
        r = StaggerGroupResolver(
            self.split,
            tags_by_printer={},
            tag_names={},
            location_by_printer={5: 30},
            parent_by_location={30: 31, 31: 30, 10: None, 20: None},
            location_names={30: "x", 31: "y", **LOCS},
        )
        assert r.groups_for(5) == {(None, 10), (None, 20)}


class TestBothAxes:
    split = StaggerSplit(by_tags=True, tag_ids=frozenset({1, 2}), by_location=True, location_ids=frozenset({10, 20}))

    def test_the_key_is_the_product(self):
        r = _resolver(self.split, tags_by_printer={5: {1}}, location_by_printer={5: 20})
        assert r.groups_for(5) == {(1, 20)}
        assert r.label((1, 20)) == "Фаза 1 · Цех B"
        assert len(r.universe) == 4

    def test_wildcard_on_one_axis_only(self):
        r = _resolver(self.split, tags_by_printer={5: {2}}, location_by_printer={5: None})
        assert r.groups_for(5) == {(2, 10), (2, 20)}
        assert r.is_wildcard(5)


class TestParse:
    def test_json_arrays_of_ints(self):
        assert parse_id_list("[3, 1]") == {1, 3}
        assert parse_id_list("[]") == frozenset()
        assert parse_id_list(None) == frozenset()
        assert parse_id_list("None") == frozenset()

    def test_anything_else_is_nothing(self):
        assert parse_id_list("not json") == frozenset()
        assert parse_id_list('{"a": 1}') == frozenset()
        assert parse_id_list("[true]") == frozenset()
        assert parse_id_list('["1"]') == frozenset()
