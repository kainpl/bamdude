"""Unit tests for the 1-bit label raster (direct-to-device label printing)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend.app.services.label_raster import (
    choose_layout,
    render_label_png,
    render_label_raster,
)
from backend.app.services.label_renderer import LabelData

# 203 dpi, which is what a B1 and most of the family run at. Passed explicitly
# in every test below, because it is a property of the device rather than a
# constant — see ``test_resolution_is_a_parameter_not_a_constant``.
DPMM_203 = 8.0
DPMM_300 = 300 / 25.4


def _sample(**overrides) -> LabelData:
    return LabelData(
        spool_id=overrides.pop("spool_id", 42),
        name=overrides.pop("name", "Polymaker Ivory"),
        material=overrides.pop("material", "PLA"),
        brand=overrides.pop("brand", "Polymaker"),
        subtype=overrides.pop("subtype", "Matte"),
        rgba=overrides.pop("rgba", "F5E6D3FF"),
        storage_location=overrides.pop("storage_location", None),
        deeplink_url=overrides.pop("deeplink_url", "https://example.test/inventory?spool=42"),
    )


def _black_ratio(img: Image.Image) -> float:
    px = list(img.convert("L").getdata())
    return sum(1 for p in px if p == 0) / len(px)


def test_pixel_size_follows_millimetres_at_the_given_resolution():
    img = render_label_raster(_sample(), width_mm=40, height_mm=20, dots_per_mm=DPMM_203)
    assert img.size == (320, 160)


def test_resolution_is_a_parameter_not_a_constant():
    """An earlier draft assumed every Niimbot is 8 dots/mm. That is false inside
    the set of models the bridge's first ported print flow already covers — an
    M2_H is 300 dpi — so the same label in millimetres is a different number of
    pixels depending on the device that will print it.
    """
    at_203 = render_label_raster(_sample(), width_mm=40, height_mm=20, dots_per_mm=DPMM_203)
    at_300 = render_label_raster(_sample(), width_mm=40, height_mm=20, dots_per_mm=DPMM_300)
    assert at_203.size != at_300.size
    assert at_300.size == (472, 236)


def test_a_width_that_is_not_a_whole_byte_is_padded_up_not_down():
    """The wire format packs eight pixels to a byte. Rounding down would crop
    the label; rounding up adds blank columns the printer ignores.
    """
    img = render_label_raster(_sample(), width_mm=25.1, height_mm=20, dots_per_mm=DPMM_203)
    assert img.width % 8 == 0
    assert img.width >= round(25.1 * DPMM_203)


def test_output_is_one_bit_with_no_intermediate_grey():
    """Antialiased text is exactly the muddy edge #1870 was fighting on 203 dpi
    heads. Drawing straight onto a "1" image is what prevents it, so assert the
    absence of grey rather than the mode alone.
    """
    img = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203)
    assert img.mode == "1"
    assert set(img.convert("L").getdata()) <= {0, 255}


def test_roomy_layout_carries_a_qr_and_tight_does_not():
    roomy = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203, layout="roomy")
    tight = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203, layout="tight")
    assert _black_ratio(roomy) > _black_ratio(tight)


def test_layout_is_chosen_automatically_when_not_given():
    # A 40 x 30 mm label at 203 dpi has room for a scannable code; a 20 x 8 does
    # not, and squeezing one in would print something nothing can read.
    assert choose_layout(round(40 * DPMM_203), round(30 * DPMM_203)) == "roomy"
    assert choose_layout(round(20 * DPMM_203), round(8 * DPMM_203)) == "tight"


def test_an_explicit_layout_overrides_the_automatic_choice():
    """An operator who wants text only on a large label is not wrong."""
    img = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203, layout="tight")
    auto = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203)
    assert _black_ratio(img) < _black_ratio(auto)


def test_a_cyrillic_name_renders_without_raising_and_marks_pixels():
    """The PDF renderer silently substituted Dingbats here before the font fix.
    PIL raises instead of substituting, so the assertion is that it does not.
    """
    img = render_label_raster(
        _sample(name="Чорний матовий", brand="Пластик Україна", storage_location="Полиця 3"),
        width_mm=40,
        height_mm=30,
        dots_per_mm=DPMM_203,
    )
    assert _black_ratio(img) > 0.01


def test_rotation_swaps_the_axes():
    img = render_label_raster(_sample(), width_mm=40, height_mm=20, dots_per_mm=DPMM_203, rotate=90)
    assert img.size == (160, 320)


@pytest.mark.parametrize("rotate", [45, -90, 360])
def test_unsupported_rotation_is_refused(rotate):
    with pytest.raises(ValueError):
        render_label_raster(_sample(), width_mm=40, height_mm=20, dots_per_mm=DPMM_203, rotate=rotate)


def test_png_bytes_round_trip_to_the_same_image():
    raw = render_label_png(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203)
    img = Image.open(io.BytesIO(raw))
    assert img.size == (320, 240)
    assert img.mode == "1"


def test_a_label_too_small_to_hold_anything_still_produces_an_image():
    """Nothing downstream copes with None, and a 6 mm label is a real cassette.
    Several marks are placed by subtraction, so this pins the arithmetic.
    """
    img = render_label_raster(_sample(), width_mm=6, height_mm=6, dots_per_mm=DPMM_203)
    assert img.size[0] % 8 == 0
    assert img.mode == "1"


def test_a_missing_deeplink_drops_the_code_rather_than_drawing_an_empty_one():
    """A QR of nothing scans as nothing, which is worse than the space it took."""
    img = render_label_raster(_sample(deeplink_url=""), width_mm=40, height_mm=30, dots_per_mm=DPMM_203, layout="roomy")
    with_code = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203, layout="roomy")
    assert _black_ratio(img) < _black_ratio(with_code)


def test_the_colour_swatch_never_appears():
    """Not a simplification — the same conclusion ``monochrome=True`` already
    reached for thermal output. A swatch prints as a grey smear and the hex line
    carries the colour anyway. Two very different colours must render alike.
    """
    dark = render_label_raster(_sample(rgba="000000FF"), width_mm=40, height_mm=30, dots_per_mm=DPMM_203)
    light = render_label_raster(_sample(rgba="FFFFFFFF"), width_mm=40, height_mm=30, dots_per_mm=DPMM_203)
    assert abs(_black_ratio(dark) - _black_ratio(light)) < 0.005


# ── Found by looking at the output, then pinned ──


def test_the_material_survives_a_line_that_has_to_shrink():
    """The details line is brand + material + subtype, and on a narrow column it
    does not fit. Cutting the joined string by characters loses the tail — and
    the tail is the material, which is the one field somebody is looking for
    when they pick a spool off a shelf. Parts are dropped whole instead.
    """
    from PIL import Image, ImageDraw

    from backend.app.services.label_raster import _compose_details
    from backend.app.services.label_raster_fonts import font_at

    draw = ImageDraw.Draw(Image.new("1", (10, 10), 1))
    font = font_at(14)
    data = _sample(brand="A Very Long Brand Name Indeed", material="PETG", subtype="Matte")

    wide = _compose_details(draw, data, font, 600)
    narrow = _compose_details(draw, data, font, 90)

    assert "PETG" in wide and "Matte" in wide
    assert "PETG" in narrow, "the material is the field that must not be dropped"
    assert "…" not in narrow, "parts are dropped whole, never cut mid-string"


def test_a_spool_with_only_a_material_still_gets_a_details_line():
    from PIL import Image, ImageDraw

    from backend.app.services.label_raster import _compose_details
    from backend.app.services.label_raster_fonts import font_at

    draw = ImageDraw.Draw(Image.new("1", (10, 10), 1))
    data = _sample(brand=None, subtype=None, material="PLA")
    assert _compose_details(draw, data, font_at(14), 200) == "PLA"


def test_the_text_block_is_centred_against_the_code_beside_it():
    """The QR is centred on its side of the label, so a text column pinned to
    the top leaves one band of white beneath it and reads as unfinished. Both
    halves of the leftover space should be equal, within a pixel of rounding.
    """
    img = render_label_raster(_sample(), width_mm=40, height_mm=30, dots_per_mm=DPMM_203, layout="roomy")
    px = img.convert("L").load()
    # Only look at the text column, left of where the code can reach.
    text_cols = range(0, img.width // 2)
    rows_with_ink = [y for y in range(img.height) if any(px[x, y] == 0 for x in text_cols)]
    assert rows_with_ink, "the text column drew nothing at all"
    above = rows_with_ink[0]
    below = img.height - 1 - rows_with_ink[-1]
    assert abs(above - below) <= max(4, img.height // 20)
