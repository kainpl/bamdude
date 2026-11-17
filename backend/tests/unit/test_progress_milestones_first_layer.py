"""Progress milestones count from the first layer, and from the run they belong to.

An A1 mini reports 85 % in its first RUNNING frame while it is still heating at
layer 0: the 75 % notice went out beside "print started" and 25 / 50 could never
follow. And a print that followed another without a frame under 5 % inherited
the finished print's 75 and got no milestone at all (upstream #3211).
"""

import pytest

from backend.app import main
from backend.app.main import _progress_milestone_to_notify as milestone


@pytest.mark.parametrize(("progress", "expected"), [(10, 0), (25, 25), (49, 25), (50, 50), (75, 75), (99, 75)])
def test_the_milestone_of_a_printing_layer(progress, expected):
    assert milestone(progress, layer_num=3, total_layers=120, stg_cur=0, last=0) == expected


def test_nothing_counts_before_the_first_layer_when_layers_are_reported():
    assert milestone(85, layer_num=0, total_layers=120, stg_cur=0, last=0) == 0


def test_nothing_counts_in_a_preparation_stage_without_layer_data():
    assert milestone(85, layer_num=0, total_layers=0, stg_cur=2, last=0) == 0


@pytest.mark.parametrize("stage", [0, -1, 255])
def test_a_printer_that_reports_no_layers_is_not_held_back(stage):
    assert milestone(50, layer_num=0, total_layers=0, stg_cur=stage, last=0) == 50


def test_a_milestone_already_sent_is_not_sent_again():
    assert milestone(60, layer_num=5, total_layers=120, stg_cur=0, last=50) == 0
    assert milestone(80, layer_num=5, total_layers=120, stg_cur=0, last=50) == 75


def test_no_progress_no_milestone():
    assert milestone(0, layer_num=5, total_layers=120, stg_cur=0, last=0) == 0


@pytest.fixture
def memory():
    stores = (main._last_progress_milestone, main._progress_milestone_run)
    saved = [dict(s) for s in stores]
    for s in stores:
        s.clear()
    yield
    for s, old in zip(stores, saved, strict=True):
        s.clear()
        s.update(old)


def test_a_new_run_starts_its_milestones_over(memory):
    main._last_progress_milestone[4] = 75
    main._progress_milestone_run[4] = 100

    main._milestones_follow_run(4, 101)

    assert main._last_progress_milestone[4] == 0


def test_the_same_run_bound_again_keeps_its_milestones(memory):
    """A reconnect binds the run it already has: 25 and 50 must not repeat."""
    main._milestones_follow_run(4, 100)
    main._last_progress_milestone[4] = 50

    main._milestones_follow_run(4, 100)

    assert main._last_progress_milestone[4] == 50


def test_the_first_run_bound_after_a_restart_keeps_what_was_just_announced(memory):
    main._last_progress_milestone[4] = 50

    main._milestones_follow_run(4, 100)

    assert main._last_progress_milestone[4] == 50


def test_both_stores_leave_with_a_deleted_printer():
    from backend.app.services import printer_deletion

    names = {name for _module, name in printer_deletion._ID_KEYED}
    assert {"_last_progress_milestone", "_progress_milestone_run"} <= names
