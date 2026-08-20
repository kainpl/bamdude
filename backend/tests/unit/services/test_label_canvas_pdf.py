"""The template walker, exercised through the PDF backend."""

from __future__ import annotations

import io
import re

from backend.app.services.label_renderer import render_template_pdf, render_template_sheet_pdf
from backend.app.services.label_template import LabelSheetSpec, LabelTemplateSpec

CONTEXT = {
    "display_name": "Polymaker PLA",
    "material": "PLA",
    "ean": "200000000042",
    "deeplink": "https://bam.example/inventory?spool=42",
}


def _spec(*elements, width_mm=40.0, height_mm=30.0) -> LabelTemplateSpec:
    return LabelTemplateSpec(name="T", width_mm=width_mm, height_mm=height_mm, elements=list(elements))


def _text(**overrides) -> dict:
    return {
        "type": "text",
        "x_mm": 2,
        "y_mm": 2,
        "w_mm": 30,
        "h_mm": 6,
        "content": "{display_name}",
        **overrides,
    }


def test_a_template_renders_a_valid_pdf():
    pdf, warnings = render_template_pdf(_spec(), [CONTEXT])
    assert pdf.startswith(b"%PDF-")
    assert not warnings


def test_one_page_per_spool():
    pdf, _ = render_template_pdf(_spec(_text()), [CONTEXT, CONTEXT, CONTEXT])
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 3


def test_the_page_is_the_size_of_the_label():
    """40 × 30 mm in points, to the nearest point: 113.4 × 85.0."""
    pdf, _ = render_template_pdf(_spec(), [CONTEXT])
    box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", pdf)
    assert box, "no MediaBox in the PDF"
    width, height = float(box.group(1)), float(box.group(2))
    assert abs(width - 113.4) < 1
    assert abs(height - 85.0) < 1


def test_text_is_drawn_with_the_vendored_font_not_a_built_in_face():
    """⚠️ The built-in Type-1 faces substitute Dingbats for Cyrillic — see
    `inv-labels-need-a-unicode-font` in the vault. This backend must never name
    one, and a Ukrainian spool is how you find out that it did.
    """
    pdf, _ = render_template_pdf(_spec(_text()), [{"display_name": "Чорний матовий"}])
    assert b"ZapfDingbats" not in pdf
    assert b"Arimo" in pdf


def test_a_qr_element_reaches_the_pdf_as_an_image():
    pdf, _ = render_template_pdf(
        _spec({"type": "qr", "x_mm": 28, "y_mm": 2, "w_mm": 10, "h_mm": 10, "content": "{deeplink}"}),
        [CONTEXT],
    )
    assert b"/Image" in pdf


def test_a_barcode_element_reaches_the_pdf_as_an_image():
    """⚠️ Raster, not reportlab's vector symbologies. Two implementations would
    let one label differ by how it was printed, which is what having one
    template is for.
    """
    pdf, _ = render_template_pdf(
        _spec(
            {
                "type": "barcode",
                "x_mm": 2,
                "y_mm": 20,
                "w_mm": 36,
                "h_mm": 8,
                "content": "{ean}",
                "symbology": "ean13",
            }
        ),
        [CONTEXT],
    )
    assert b"/Image" in pdf


def test_an_element_past_the_edge_is_reported_here_too():
    _, warnings = render_template_pdf(_spec(_text(x_mm=36, w_mm=20)), [CONTEXT])
    assert any("past the label" in w for w in warnings)


def test_warnings_are_not_repeated_once_per_spool():
    """The same template drawn twenty times has one fault, not twenty. A list
    that grows with the batch buries whatever else is in it.
    """
    _, warnings = render_template_pdf(_spec(_text(x_mm=36, w_mm=20)), [CONTEXT] * 20)
    assert len(warnings) == 1


def test_a_bad_barcode_payload_warns_once_and_still_renders_every_page():
    _, warnings = render_template_pdf(
        _spec(
            {
                "type": "barcode",
                "x_mm": 2,
                "y_mm": 20,
                "w_mm": 36,
                "h_mm": 8,
                "content": "{display_name}",
                "symbology": "ean13",
            }
        ),
        [CONTEXT, CONTEXT],
    )
    assert len(warnings) == 1
    assert "ean13" in warnings[0]


def test_the_origin_is_the_top_left_of_the_label_not_the_bottom_left():
    """⚠️ reportlab counts from the bottom of the page; a template counts from
    the top of the label. Getting the flip wrong mirrors every label vertically
    and reads as a layout bug rather than a coordinate one.

    Tested as arithmetic rather than by grepping the output: reportlab
    compresses its content streams, and a conversion that can be silently wrong
    is no place to rely on being able to read them.
    """
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas

    from backend.app.services.label_renderer import PdfCanvas

    page_height = 30 * mm  # a 30 mm tall label
    canvas = PdfCanvas(
        rl_canvas.Canvas(io.BytesIO(), pagesize=(40 * mm, page_height)),
        origin_mm=(0.0, 0.0),
        page_height_pt=page_height,
    )

    # A box 1 mm from the top, 4 mm tall, has its *bottom* 25 mm above the
    # page's bottom edge — not 1 mm.
    _, bottom, _, height = canvas.box_to_points((2.0, 1.0, 10.0, 4.0))
    assert abs(bottom - 25 * mm) < 0.01
    assert abs(height - 4 * mm) < 0.01

    # And a box at the bottom of the label lands near the bottom of the page.
    _, low_bottom, _, _ = canvas.box_to_points((2.0, 25.0, 10.0, 4.0))
    assert abs(low_bottom - 1 * mm) < 0.01
    assert low_bottom < bottom


def test_a_sheet_cell_offsets_the_whole_label():
    """The same box, in the second column of the second row, is further right
    and further down the page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas

    from backend.app.services.label_renderer import PdfCanvas

    page_height = A4[1]
    c = rl_canvas.Canvas(io.BytesIO(), pagesize=A4)
    first = PdfCanvas(c, origin_mm=(10.0, 10.0), page_height_pt=page_height)
    second = PdfCanvas(c, origin_mm=(80.0, 50.0), page_height_pt=page_height)

    left_a, bottom_a, _, _ = first.box_to_points((0.0, 0.0, 10.0, 5.0))
    left_b, bottom_b, _, _ = second.box_to_points((0.0, 0.0, 10.0, 5.0))
    assert left_b > left_a
    assert bottom_b < bottom_a


def test_a_sheet_lays_the_same_label_out_in_a_grid():
    sheet = LabelSheetSpec(
        name="Avery 5160",
        page_size="letter",
        cell_width_mm=66.675,
        cell_height_mm=25.4,
        cols=3,
        rows=10,
        margin_top_mm=12.7,
        margin_left_mm=4.76,
        gap_x_mm=3.175,
        gap_y_mm=0.0,
    )
    spec = _spec(_text(), width_mm=66.675, height_mm=25.4)
    pdf, _ = render_template_sheet_pdf(spec, [CONTEXT] * 31, sheet)
    # Thirty per page, so thirty-one spools need two.
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 2


def test_a_sheet_page_is_the_paper_not_the_label():
    sheet = LabelSheetSpec(
        name="A4",
        page_size="A4",
        cell_width_mm=63.5,
        cell_height_mm=38.1,
        cols=3,
        rows=7,
        margin_top_mm=15.15,
        margin_left_mm=7.0,
        gap_x_mm=2.5,
        gap_y_mm=0.0,
    )
    pdf, _ = render_template_sheet_pdf(_spec(width_mm=63.5, height_mm=38.1), [CONTEXT], sheet)
    box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", pdf)
    # A4 is 595 × 842 pt.
    assert abs(float(box.group(1)) - 595) < 2
    assert abs(float(box.group(2)) - 842) < 2


def test_an_empty_batch_still_produces_a_readable_pdf():
    """Callers short-circuit on an empty selection, but a renderer that returns
    a corrupt file for one is a trap for whoever forgets to."""
    pdf, warnings = render_template_pdf(_spec(_text()), [])
    assert pdf.startswith(b"%PDF-")
    assert not warnings


def test_a_swatch_reaches_the_pdf_as_colour():
    """The PDF keeps the colour block the fixed layouts have always drawn.
    Losing it would be losing the thing people find a spool by on a shelf.
    """
    pdf, warnings = render_template_pdf(
        _spec({"type": "swatch", "x_mm": 2, "y_mm": 2, "w_mm": 6, "h_mm": 20, "content": "{color_hex_all}"}),
        [{"color_hex_all": "FF3300"}],
    )
    assert pdf.startswith(b"%PDF-")
    assert not warnings


def test_a_two_colour_spool_gets_one_band_per_colour():
    """Painting only the first colour is a small lie somebody reaches for on a
    shelf — the same reasoning that put segments on the printer card.

    ⚠️ Counted in the content stream rather than by comparing file sizes. The
    first version of this test did the latter and passed nothing: reportlab
    compresses, and one rectangle and two came out the same length.
    """
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas

    from backend.app.services.label_renderer import PdfCanvas

    def fills(colours: list[str]) -> int:
        buf = io.BytesIO()
        # pageCompression=0 so the operators are readable ASCII.
        c = rl_canvas.Canvas(buf, pagesize=(40 * mm, 30 * mm), pageCompression=0)
        PdfCanvas(c, origin_mm=(0.0, 0.0), page_height_pt=30 * mm).swatch(colours, box_mm=(2.0, 2.0, 6.0, 20.0))
        c.showPage()
        c.save()
        # "re f" is a filled rectangle.
        # reportlab writes a filled rectangle as "re f*" — read off the real
        # output rather than guessed at, which is how the first two attempts
        # at this test managed to assert nothing.
        return buf.getvalue().count(b"re f*")

    assert fills(["FF3300"]) == 1
    assert fills(["FF3300", "FFFFFF"]) == 2
    assert fills(["FF3300", "FFFFFF", "0000FF"]) == 3
