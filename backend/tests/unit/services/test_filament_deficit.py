"""Will this print run the spool out — and the cases where saying so is wrong.

Ported from upstream #2779 + `df5aa04d`, but the decision here is ours: the
answer **warns and never blocks**. A farm finishes a spool mid-plate and swaps
it; refusing to dispatch would stop work the operator intended.

Most of these tests are about **not** warning. A warning that cries wolf is
worse than none, because the next one is ignored too — and three of the four
ways to cry wolf here are silent-data problems, not logic ones.
"""

from __future__ import annotations

from backend.app.services.filament_deficit import compute_shortfalls

REQ = [{"slot_id": 0, "used_grams": 20.5}]
LOADED = [
    {"global_tray_id": 0, "type": "PLA", "ams_id": 0, "tray_id": 0},
    {"global_tray_id": 1, "type": "PLA", "ams_id": 0, "tray_id": 1},
    {"global_tray_id": 2, "type": "PETG", "ams_id": 0, "tray_id": 2},
]
MAP = [0]


def _short(**kw):
    args = {
        "requirements": REQ,
        "loaded": LOADED,
        "ams_mapping": MAP,
        "remaining_by_tray": {0: 9.0},
        "auto_refill": False,
    }
    args.update(kw)
    return compute_shortfalls(**args)


class TestTheAnswerItself:
    def test_a_slot_that_cannot_finish_the_job_is_reported(self):
        [only] = _short()

        assert (only.slot_label, only.needed_grams, only.available_grams) == ("A1", 20.5, 9.0)
        assert only.missing_grams == 11.5

    def test_enough_filament_says_nothing(self):
        assert _short(remaining_by_tray={0: 50.0}) == []

    def test_exactly_enough_says_nothing(self):
        assert _short(remaining_by_tray={0: 20.5}) == []

    def test_two_slicer_slots_on_one_tray_are_summed(self):
        """Judged apart each looks satisfied; together they empty the tray."""
        got = compute_shortfalls(
            requirements=[{"slot_id": 0, "used_grams": 12.0}, {"slot_id": 1, "used_grams": 12.0}],
            loaded=LOADED,
            ams_mapping=[0, 0],
            remaining_by_tray={0: 20.0},
            auto_refill=False,
        )

        assert [s.needed_grams for s in got] == [24.0]


class TestAmsBackupIsPartOfTheAnswer:
    """With auto-refill on the AMS switches to another slot of the same
    filament, so judging a slot alone reports a shortfall that never happens."""

    def test_a_backup_slot_covers_the_gap(self):
        assert _short(remaining_by_tray={0: 9.0, 1: 50.0}, auto_refill=True) == []

    def test_but_only_when_auto_refill_is_actually_on(self):
        assert len(_short(remaining_by_tray={0: 9.0, 1: 50.0}, auto_refill=False)) == 1

    def test_a_different_filament_is_not_a_backup(self):
        assert len(_short(remaining_by_tray={0: 9.0, 2: 50.0}, auto_refill=True)) == 1

    def test_a_slot_this_print_also_uses_is_not_spare_capacity(self):
        """Counting it twice would hide a real shortfall."""
        got = compute_shortfalls(
            requirements=[{"slot_id": 0, "used_grams": 20.0}, {"slot_id": 1, "used_grams": 20.0}],
            loaded=LOADED,
            ams_mapping=[0, 1],
            remaining_by_tray={0: 5.0, 1: 5.0},
            auto_refill=True,
        )

        assert len(got) == 2

    def test_colour_does_not_disqualify_a_backup(self):
        """The AMS refills by material. An operator who loaded two colours and
        turned auto-refill on has said that is acceptable."""
        loaded = [
            {"global_tray_id": 0, "type": "PLA", "ams_id": 0, "tray_id": 0, "color": "FF0000"},
            {"global_tray_id": 1, "type": "PLA", "ams_id": 0, "tray_id": 1, "color": "0000FF"},
        ]

        assert _short(loaded=loaded, remaining_by_tray={0: 9.0, 1: 50.0}, auto_refill=True) == []


class TestSilenceIsNotEmptiness:
    def test_a_tray_with_no_figure_is_skipped(self):
        """Most spools are not RFID and most installs do not track every one.
        Treating silence as zero would warn on nearly every print."""
        assert _short(remaining_by_tray={}) == []

    def test_no_mapping_means_nothing_to_judge(self):
        assert _short(ams_mapping=None) == []

    def test_an_unmapped_slot_is_skipped(self):
        assert _short(ams_mapping=[-1]) == []

    def test_a_requirement_with_no_weight_is_skipped(self):
        assert (
            compute_shortfalls(
                requirements=[{"slot_id": 0, "used_grams": 0}],
                loaded=LOADED,
                ams_mapping=MAP,
                remaining_by_tray={0: 1.0},
                auto_refill=False,
            )
            == []
        )

    def test_a_mapping_shorter_than_the_requirements_does_not_raise(self):
        assert (
            compute_shortfalls(
                requirements=[{"slot_id": 5, "used_grams": 10.0}],
                loaded=LOADED,
                ams_mapping=MAP,
                remaining_by_tray={0: 1.0},
                auto_refill=False,
            )
            == []
        )
