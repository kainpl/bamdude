"""Every tree question lives in one helper.

Loaded whole and walked in Python rather than asked with a recursive CTE: this
table holds tens of rows on the largest farm, so one small SELECT is cheaper
than the recursion and far easier to test. At thousands of rows the answer
would be the opposite.
"""

from backend.app.services.printer_location_service import (
    LocationNode,
    depth_of,
    path_of,
    subtree_ids,
    would_cycle,
)

# Workshop(1) -> Shelf 1(2) -> Box(4)
#             -> Shelf 2(3)
# Hall(5)
TREE = {
    1: LocationNode(id=1, name="Workshop", parent_id=None),
    2: LocationNode(id=2, name="Shelf 1", parent_id=1),
    3: LocationNode(id=3, name="Shelf 2", parent_id=1),
    4: LocationNode(id=4, name="Box", parent_id=2),
    5: LocationNode(id=5, name="Hall", parent_id=None),
}


def test_a_subtree_includes_its_own_root():
    # "Aim the work at the workshop" has to reach the workshop's own printers
    # as well as the shelves'.
    assert subtree_ids(TREE, 1) == {1, 2, 3, 4}


def test_a_leaf_is_its_own_subtree():
    assert subtree_ids(TREE, 4) == {4}


def test_a_subtree_of_an_unknown_id_is_empty_not_an_error():
    """A location deleted between two requests must not 500 the queue."""
    assert subtree_ids(TREE, 999) == set()


def test_the_path_reads_from_the_root_down():
    assert path_of(TREE, 4) == "Workshop / Shelf 1 / Box"
    assert path_of(TREE, 1) == "Workshop"


def test_depth_counts_from_one():
    assert depth_of(TREE, 1) == 1
    assert depth_of(TREE, 2) == 2
    assert depth_of(TREE, 4) == 3


def test_a_location_cannot_become_its_own_descendant():
    # Workshop under Box would make a ring, and walking it never ends -- the
    # first list request would hang the process rather than fail.
    assert would_cycle(TREE, 1, 4) is True
    assert would_cycle(TREE, 1, 1) is True


def test_an_ordinary_move_is_not_a_cycle():
    assert would_cycle(TREE, 4, 3) is False
    assert would_cycle(TREE, 4, None) is False


def test_a_broken_parent_link_terminates():
    """A row pointing at a parent that is gone. Nothing should loop, and the
    path should say what it still knows."""
    orphaned = {7: LocationNode(id=7, name="Lost", parent_id=999)}

    assert path_of(orphaned, 7) == "Lost"
    assert depth_of(orphaned, 7) == 1
