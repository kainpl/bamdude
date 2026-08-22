"""A sheet's grid has to fit the paper it claims, and the paper has to be real.

Two failures that only show up on printed stock:

* a grid wider than its page prints its last column half off the paper, and
  nothing on screen said so;
* an unknown page size fell through to Letter silently, so an A5 sheet laid
  itself out on Letter and every cell landed in the wrong place.

Both are cheap to refuse and expensive to discover — the discovery costs a
sheet of adhesive stock.
"""

import pytest

from backend.app.services.label_renderer import PAGE_SIZES_MM, page_size_points
from backend.app.services.label_template import LabelSheetSpec, sheet_overflow


def _sheet(**overrides) -> LabelSheetSpec:
    base = {
        "name": "test sheet",
        "page_size": "A4",
        "cell_width_mm": 63.5,
        "cell_height_mm": 38.1,
        "cols": 3,
        "rows": 7,
        "margin_top_mm": 15.0,
        "margin_left_mm": 7.0,
        "gap_x_mm": 2.5,
        "gap_y_mm": 0.0,
    }
    base.update(overrides)
    return LabelSheetSpec(**base)


class TestThePaperIsReal:
    def test_every_offered_size_resolves(self):
        for name in PAGE_SIZES_MM:
            width, height = page_size_points(name)
            assert width > 0 and height > 0

    def test_a5_is_not_letter(self):
        """⚠️ It used to be. The resolver was `A4 if name == "A4" else letter`,
        so every size that was not A4 became Letter — quietly, and only on
        paper."""
        assert page_size_points("A5") != page_size_points("letter")

    def test_an_unknown_size_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError, match="A5"):
            page_size_points("A6")


class TestTheGridFitsThePage:
    def test_a_sheet_that_fits_reports_nothing(self):
        assert sheet_overflow(_sheet()) == []

    def test_too_many_columns_are_named(self):
        overflow = sheet_overflow(_sheet(cols=4))

        assert overflow, "four 63.5mm columns cannot fit across A4"
        assert any("width" in message for message in overflow)

    def test_too_many_rows_are_named(self):
        overflow = sheet_overflow(_sheet(rows=9))

        assert any("height" in message for message in overflow)

    def test_the_margin_counts(self):
        """A grid that fits from zero can still fall off once it is inset."""
        assert sheet_overflow(_sheet(margin_left_mm=60.0))

    def test_the_gaps_count_between_cells_only(self):
        """⚠️ Three columns have two gaps, not three.

        Counting a trailing gap refuses sheets that fit, which is the same
        failure as accepting ones that do not — just quieter.
        """
        # 3 × 63.5 + 2 × 2.5 + 7 = 203.5mm, inside A4's 210.
        assert sheet_overflow(_sheet(gap_x_mm=2.5)) == []
        # A third gap would push it to 206 and still fit, so prove the count
        # directly at the edge instead.
        assert sheet_overflow(_sheet(cols=2, cell_width_mm=100.0, margin_left_mm=5.0, gap_x_mm=5.0)) == []
        assert sheet_overflow(_sheet(cols=2, cell_width_mm=100.0, margin_left_mm=5.0, gap_x_mm=6.0))

    def test_a5_is_judged_against_a5(self):
        """The grid that fits A4 does not fit A5 — and now it is told so."""
        assert sheet_overflow(_sheet(page_size="A4")) == []
        assert sheet_overflow(_sheet(page_size="A5"))
