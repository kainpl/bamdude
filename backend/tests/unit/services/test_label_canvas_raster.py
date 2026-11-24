"""The template walker, exercised through the raster backend."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageOps

from backend.app.services.label_raster import render_template_png, render_template_raster
from backend.app.services.label_template import LabelTemplateSpec

DPMM = 8.0
CONTEXT = {
    "display_name": "Polymaker PLA",
    "material": "PLA",
    "ean": "200000000042",
    "deeplink": "https://bam.example/inventory?spool=42",
}


def _spec(*elements, width_mm=40.0, height_mm=30.0) -> LabelTemplateSpec:
    return LabelTemplateSpec(name="T", width_mm=width_mm, height_mm=height_mm, elements=list(elements))


def _ink(img: Image.Image, box_px: tuple[int, int, int, int]) -> float:
    """Fraction of dark pixels inside a box, so a test can say *where* ink went."""
    x, y, w, h = box_px
    crop = img.convert("L").crop((x, y, x + w, y + h))
    px = list(crop.getdata())
    return sum(1 for p in px if p == 0) / max(len(px), 1)


def test_an_empty_template_renders_a_blank_label_of_the_right_size():
    img, warnings = render_template_raster(_spec(), CONTEXT, dots_per_mm=DPMM)
    assert img.size == (320, 240)
    assert img.mode == "1"
    assert not warnings
    assert _ink(img, (0, 0, img.width, img.height)) == 0


def test_a_text_element_puts_ink_where_its_box_is_and_nowhere_else():
    img, _ = render_template_raster(
        _spec(
            {
                "type": "text",
                "x_mm": 2,
                "y_mm": 2,
                "w_mm": 20,
                "h_mm": 6,
                "content": "{display_name}",
                "size_mm": 4,
            }
        ),
        CONTEXT,
        dots_per_mm=DPMM,
    )
    assert _ink(img, (16, 16, 160, 48)) > 0.02
    # The bottom half of the label was not asked for anything.
    assert _ink(img, (0, 160, 320, 80)) == 0


def test_a_qr_element_lands_inside_its_box():
    img, _ = render_template_raster(
        _spec({"type": "qr", "x_mm": 28, "y_mm": 2, "w_mm": 10, "h_mm": 10, "content": "{deeplink}"}),
        CONTEXT,
        dots_per_mm=DPMM,
    )
    assert _ink(img, (224, 16, 80, 80)) > 0.1
    assert _ink(img, (0, 0, 200, 240)) == 0


def test_a_barcode_element_lands_inside_its_box():
    img, _ = render_template_raster(
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
        CONTEXT,
        dots_per_mm=DPMM,
    )
    assert _ink(img, (16, 160, 288, 64)) > 0.1
    assert _ink(img, (0, 0, 320, 150)) == 0


def test_a_barcode_that_cannot_encode_its_payload_warns_instead_of_raising():
    """One bad element must not cost the whole label. The gap is visible and the
    reason is in the warnings, which beats a 500 on a print button.
    """
    img, warnings = render_template_raster(
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
        CONTEXT,
        dots_per_mm=DPMM,
    )
    assert img.size == (320, 240)
    assert any("ean13" in w for w in warnings)


def test_an_element_past_the_edge_is_clipped_and_reported():
    """Not refused — bleeding off the edge can be deliberate — but the caller is
    told, because a silently cropped label reads as a rendering bug.
    """
    img, warnings = render_template_raster(
        _spec({"type": "text", "x_mm": 36, "y_mm": 2, "w_mm": 20, "h_mm": 6, "content": "{display_name}"}),
        CONTEXT,
        dots_per_mm=DPMM,
    )
    assert img.size == (320, 240)
    assert any("past the label" in w for w in warnings)


def test_the_same_template_is_bigger_on_a_finer_head():
    coarse, _ = render_template_raster(_spec(), CONTEXT, dots_per_mm=8.0)
    fine, _ = render_template_raster(_spec(), CONTEXT, dots_per_mm=300 / 25.4)
    assert fine.size[0] > coarse.size[0]


def _ink_rows(img: Image.Image, box_px: tuple[int, int, int, int]) -> int:
    """How many pixel rows inside a box carry any ink — a stand-in for type size."""
    x, y, w, h = box_px
    px = img.convert("L").load()
    return sum(1 for row in range(y, y + h) if any(px[c, row] == 0 for c in range(x, x + w)))


def test_shrink_sets_smaller_type_where_clip_keeps_the_size_and_cuts():
    """⚠️ The obvious assertion — that shrinking puts *more ink* in the box —
    is false, and this test failed on it first. Shrinking fits more characters
    but draws each one smaller, so the total can easily be less. What actually
    separates the two is the height of the type, which is the thing `shrink`
    changes and `clip` does not.
    """
    long_text = {"display_name": "A very long spool name that will never fit"}
    element = {
        "type": "text",
        "x_mm": 2,
        "y_mm": 2,
        "w_mm": 20,
        "h_mm": 6,
        "content": "{display_name}",
        "size_mm": 5,
    }
    shrunk, _ = render_template_raster(_spec({**element, "fit": "shrink"}), long_text, dots_per_mm=DPMM)
    clipped, _ = render_template_raster(_spec({**element, "fit": "clip"}), long_text, dots_per_mm=DPMM)

    box = (16, 16, 160, 48)
    assert _ink_rows(shrunk, box) < _ink_rows(clipped, box)
    # Both still put something there — shrinking to nothing would be a bug.
    assert _ink(shrunk, box) > 0


def test_neither_fit_lets_text_escape_its_box():
    """The whole point of a box. `clip` in particular must cut rather than run
    over the element beside it.
    """
    long_text = {"display_name": "A very long spool name that will never fit"}
    for fit in ("shrink", "clip"):
        img, _ = render_template_raster(
            _spec(
                {
                    "type": "text",
                    "x_mm": 2,
                    "y_mm": 2,
                    "w_mm": 20,
                    "h_mm": 6,
                    "content": "{display_name}",
                    "size_mm": 5,
                    "fit": fit,
                }
            ),
            long_text,
            dots_per_mm=DPMM,
        )
        # Right of the box (x > 22 mm) must be untouched.
        assert _ink(img, (180, 0, 140, 240)) == 0, fit


def test_alignment_moves_the_ink_within_the_box():
    element = {
        "type": "text",
        "x_mm": 2,
        "y_mm": 2,
        "w_mm": 30,
        "h_mm": 6,
        "content": "PLA",
        "size_mm": 4,
    }
    left, _ = render_template_raster(_spec({**element, "align": "left"}), CONTEXT, dots_per_mm=DPMM)
    right, _ = render_template_raster(_spec({**element, "align": "right"}), CONTEXT, dots_per_mm=DPMM)
    # Left-aligned puts ink near the start of the box, right-aligned near its end.
    assert _ink(left, (16, 16, 60, 48)) > _ink(right, (16, 16, 60, 48))
    assert _ink(right, (196, 16, 60, 48)) > _ink(left, (196, 16, 60, 48))


def test_a_cyrillic_name_renders():
    img, _ = render_template_raster(
        _spec({"type": "text", "x_mm": 2, "y_mm": 2, "w_mm": 30, "h_mm": 6, "content": "{display_name}"}),
        {"display_name": "Чорний матовий"},
        dots_per_mm=DPMM,
    )
    assert _ink(img, (16, 16, 240, 48)) > 0.02


def test_png_bytes_round_trip():
    import io

    raw, _ = render_template_png(
        _spec({"type": "text", "x_mm": 2, "y_mm": 2, "w_mm": 20, "h_mm": 6, "content": "{material}"}),
        CONTEXT,
        dots_per_mm=DPMM,
    )
    img = Image.open(io.BytesIO(raw))
    assert img.size == (320, 240)
    assert img.mode == "1"


def test_a_swatch_draws_nothing_on_a_thermal_head():
    """⚠️ Deliberate, not missing. A colour block on a one-bit head is a solid
    smear that says nothing — the very reason the PDF renderer already drops it
    in monochrome mode. Skipping rather than refusing means one template serves
    both a label printer and a colour sheet.
    """
    img, warnings = render_template_raster(
        _spec({"type": "swatch", "x_mm": 2, "y_mm": 2, "w_mm": 6, "h_mm": 20, "content": "{color_hex_all}"}),
        {**CONTEXT, "color_hex_all": "FF3300,FFFFFF"},
        dots_per_mm=DPMM,
    )
    assert _ink(img, (0, 0, img.width, img.height)) == 0
    assert not warnings


def test_stock_box_prints_the_continuation_in_the_raster():
    from backend.app.services.label_seed import BUILTIN_TEMPLATES

    raw = next(t for t in BUILTIN_TEMPLATES if t["builtin_key"] == "box_40x30")
    spec = LabelTemplateSpec(**{key: raw[key] for key in ("name", "width_mm", "height_mm", "elements")})
    name = next(e for e in raw["elements"] if e["content"] == "{display_name}")
    img, warnings = render_template_raster(spec, {"display_name": "Magenta Orchid"}, dots_per_mm=DPMM)
    assert not warnings
    assert img.mode == "1"
    x, y, width, size = (round(name[key] * DPMM) for key in ("x_mm", "y_mm", "w_mm", "size_mm"))
    assert _ink(img, (x, y + size + 2, width, size)) > 0.02


@pytest.mark.parametrize("dots", [8, 12])
def test_factory_wrap_keeps_all_descender_pixels(dots):
    from backend.app.services.label_raster import RasterCanvas
    from backend.app.services.label_raster_fonts import font_at
    from backend.app.services.label_seed import BUILTIN_TEMPLATES

    factory = next(t for t in BUILTIN_TEMPLATES if t["builtin_key"] == "box_40x30")
    name = next(e for e in factory["elements"] if e["content"] == "{display_name}")
    box = tuple(name[field] for field in ("x_mm", "y_mm", "w_mm", "h_mm"))
    canvas = RasterCanvas(40, 30, dots)
    canvas.text(
        "Magenta gyp",
        box_mm=box,
        size_mm=name["size_mm"],
        bold=True,
        italic=False,
        align="left",
        valign="top",
        fit="wrap",
    )

    # Draw the two words at the same font size on a separate, roomy surface.
    # Their pixel count is invariant under the integer translation into the box.
    font = font_at(round(name["size_mm"] * dots), bold=True)
    reference = Image.new("1", (40 * dots, 30 * dots), 1)
    draw = ImageDraw.Draw(reference)
    draw.text((50, 50), "Magenta", font=font, fill=0)
    draw.text((50, 130), "gyp", font=font, fill=0)
    actual = canvas.image_out()
    assert actual.histogram()[0] == reference.histogram()[0]

    x, y, w, h = (round(value * dots) for value in box)
    assert actual.crop((x, y, x + w, y + h)).histogram()[0] == actual.histogram()[0]


@pytest.mark.parametrize(("align", "valign"), [("left", "top"), ("center", "middle"), ("right", "bottom")])
def test_custom_wrap_keeps_complete_glyphs_at_each_vertical_alignment(align, valign):
    from backend.app.services.label_raster import RasterCanvas
    from backend.app.services.label_raster_fonts import font_at

    x, y, width, height = 24, 24, 116, 50
    font = font_at(23, bold=True)
    canvas = RasterCanvas(40, 30, 8)
    canvas.text(
        "Magenta gyp",
        box_mm=(x / 8, y / 8, width / 8, height / 8),
        size_mm=23 / 8,
        bold=True,
        italic=False,
        align=align,
        valign=valign,
        fit="wrap",
    )

    reference = Image.new("1", (320, 240), 1)
    draw = ImageDraw.Draw(reference)
    for line, top in zip(("Magenta", "gyp"), (50, 120), strict=True):
        remaining = width - draw.textlength(line, font=font)
        offset = {"left": 0, "center": remaining / 2, "right": remaining}[align]
        draw.text((x + offset, top), line, font=font, fill=0)
    actual = canvas.image_out()
    assert actual.histogram()[0] == reference.histogram()[0]
    assert actual.crop((x, y, x + width, y + height)).histogram()[0] == actual.histogram()[0]

    _, ink_top, _, ink_bottom = ImageOps.invert(actual.convert("L")).getbbox()
    gap_before, gap_after = ink_top - y, y + height - ink_bottom
    if valign == "middle":
        assert abs(gap_before - gap_after) <= 1
    elif valign == "bottom":
        assert gap_after == 0
    else:
        assert gap_before == 0
