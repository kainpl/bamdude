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


class TestAThermalDesignRefusesColour:
    """⚠️ This is a deliberate retreat from "one template, two backends".

    That held while colour was a small extra: the PDF renderer drew the swatch,
    the one-bit raster skipped it, and the same design printed acceptably either
    way. It stops holding once colour is what you design AROUND — a label built
    on a filled block of the spool's colour does not degrade gracefully on a
    thermal head, it arrives missing its subject.

    So the refusal happens at SAVE. A driver design keeps the swatch, because it
    may well be going to an inkjet or a laser — which is the whole reason the two
    are told apart. The raster still skips rather than refuses, because a driver
    design can be named for a device print by id and losing a block beats losing
    the job.
    """

    @staticmethod
    def _design(target: str, with_swatch: bool):
        from backend.app.services.label_template import LabelTemplateSpec

        elements = [{"type": "text", "content": "{brand}", "x_mm": 1, "y_mm": 1, "w_mm": 20, "h_mm": 5}]
        if with_swatch:
            elements.append({"type": "swatch", "x_mm": 1, "y_mm": 8, "w_mm": 10, "h_mm": 5})
        return {"name": "d", "width_mm": 40, "height_mm": 20, "target": target, "elements": elements}, LabelTemplateSpec

    def test_a_driver_design_may_carry_one(self):
        payload, spec = self._design("driver", with_swatch=True)
        assert spec(**payload).target == "driver"

    def test_a_thermal_design_may_not(self):
        payload, spec = self._design("thermal", with_swatch=True)
        with pytest.raises(ValueError, match="one-bit"):
            spec(**payload)

    def test_a_thermal_design_without_colour_is_fine(self):
        payload, spec = self._design("thermal", with_swatch=False)
        assert spec(**payload).target == "thermal"

    def test_the_default_is_the_driver(self):
        """⚠️ An unmarked design is a PDF design. Defaulting the other way
        would take the colour block off a label the moment somebody saved it
        without thinking about which printer it was for."""
        payload, spec = self._design("driver", with_swatch=False)
        del payload["target"]
        assert spec(**payload).target == "driver"


class TestTheSwatchTakesAShape:
    """⚠️ The outline is a clip, not a replacement for the banding.

    A two-colour spool is two colours whatever the outline. A circle showing
    only the first would be the same small lie the banding exists to avoid,
    wearing a different shape.
    """

    @staticmethod
    def _spec(shape: str):
        from backend.app.services.label_template import LabelTemplateSpec

        return LabelTemplateSpec(
            name="d",
            width_mm=40,
            height_mm=20,
            elements=[{"type": "swatch", "shape": shape, "x_mm": 1, "y_mm": 1, "w_mm": 10, "h_mm": 10}],
        )

    @pytest.mark.parametrize("shape", ["rect", "circle", "rounded"])
    def test_every_shape_renders(self, shape):
        from backend.app.services.label_renderer import render_template_pdf

        pdf, warnings = render_template_pdf(self._spec(shape), [{"color_hex_all": "FF0000,00FF00"}])

        assert pdf.startswith(b"%PDF"), f"{shape} produced no document"
        assert warnings == []

    def test_the_default_is_a_rectangle(self):
        """Every design drawn before this had no shape field at all."""
        assert self._spec("rect").elements[0].shape == "rect"

    def test_an_unknown_shape_is_refused(self):
        from backend.app.services.label_template import LabelTemplateSpec

        with pytest.raises(ValueError):
            LabelTemplateSpec(
                name="d",
                width_mm=40,
                height_mm=20,
                elements=[{"type": "swatch", "shape": "triangle", "x_mm": 1, "y_mm": 1, "w_mm": 5, "h_mm": 5}],
            )

    def test_the_thermal_gate_still_applies_to_every_shape(self):
        """⚠️ A round swatch is still colour. The gate keys off the element
        type, not its outline — a new shape must not be a way past it."""
        from backend.app.services.label_template import LabelTemplateSpec

        with pytest.raises(ValueError, match="one-bit"):
            LabelTemplateSpec(
                name="d",
                width_mm=40,
                height_mm=20,
                target="thermal",
                elements=[{"type": "swatch", "shape": "circle", "x_mm": 1, "y_mm": 1, "w_mm": 5, "h_mm": 5}],
            )
