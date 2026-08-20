"""Unit tests for 1D barcodes on label rasters."""

from __future__ import annotations

import pytest
from PIL import Image

from backend.app.services.label_barcode import (
    MIN_MODULE_PX,
    QUIET_ZONE_MODULES,
    SUPPORTED,
    BarcodeError,
    modules_for,
    render_barcode,
)


def _bar_runs(img: Image.Image) -> list[int]:
    """Widths of the dark runs across the middle of the image."""
    px = img.convert("L").load()
    row = img.height // 2
    runs: list[int] = []
    run = 0
    for x in range(img.width):
        if px[x, row] == 0:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return runs


def test_a_barcode_is_bilevel_like_the_rest_of_the_label():
    img = render_barcode("SPOOL-42", width_px=400, height_px=60)
    assert img.mode == "1"
    assert set(img.convert("L").getdata()) <= {0, 255}


@pytest.mark.parametrize("payload", ["42", "SPOOL-42", "A", "0000000001"])
def test_every_bar_is_a_whole_multiple_of_the_narrowest_one(payload):
    """⚠️ This is the property the whole module exists for, and the reason the
    library's own image writer is not used.

    A scanner reads the ratios between bars. The writer walks a float cursor and
    rounds each bar on its own, so a four-pixel module comes out as bars of
    three, four, eight and twelve — ratios that no longer mean what they encode.
    Drawing the module string ourselves makes the property hold by construction.

    Parametrised over several payloads on purpose: the first version of this
    test used one, and passed because that payload happened to come out even.
    """
    img = render_barcode(payload, width_px=600, height_px=60)
    runs = _bar_runs(img)
    assert runs, "no bars were drawn at all"
    narrow = min(runs)
    assert narrow >= MIN_MODULE_PX
    assert all(r % narrow == 0 for r in runs), f"bars are not multiples of {narrow}: {sorted(set(runs))}"


def test_the_barcode_fills_the_box_it_is_given():
    img = render_barcode("42", width_px=400, height_px=48)
    assert img.width == 400
    assert img.height == 48


def test_a_wider_box_gets_a_thicker_module_rather_than_a_stretched_image():
    """Not resampled — redrawn. So the narrow bar itself grows."""
    narrow_box = render_barcode("42", width_px=200, height_px=40)
    wide_box = render_barcode("42", width_px=600, height_px=40)
    assert min(_bar_runs(wide_box)) > min(_bar_runs(narrow_box))


def test_a_box_too_small_overflows_rather_than_printing_something_unreadable():
    """Fitting is worth less than scanning. Below the module floor the code
    keeps the floor and runs over its box, where the caller can see it, instead
    of shrinking into something that fits and cannot be read.
    """
    img = render_barcode("SPOOL-000042", width_px=40, height_px=40)
    assert img.width > 40
    assert min(_bar_runs(img)) >= MIN_MODULE_PX


def test_a_quiet_zone_is_left_on_both_sides():
    """A barcode with no margin is one the scanner cannot find the start of."""
    img = render_barcode("42", width_px=600, height_px=40)
    px = img.convert("L").load()
    row = img.height // 2

    first_dark = next(x for x in range(img.width) if px[x, row] == 0)
    last_dark = next(x for x in reversed(range(img.width)) if px[x, row] == 0)
    module = min(_bar_runs(img))

    assert first_dark >= QUIET_ZONE_MODULES * module
    assert img.width - 1 - last_dark >= QUIET_ZONE_MODULES * module


def test_the_module_string_comes_from_the_library_not_from_us():
    """The symbology tables are the library's job. EAN-13 is always 95 modules,
    which is a cheap way to prove we are reading a real encoder rather than
    something home-made.
    """
    assert len(modules_for("590123412345", "ean13")) == 95
    assert set(modules_for("SPOOL-42", "code128")) == {"0", "1"}


def test_a_fixed_length_symbology_refuses_a_payload_it_cannot_encode():
    """EAN-13 wants 12 or 13 digits. Swallowing this would leave a blank space
    on the label exactly where the barcode was meant to be.
    """
    with pytest.raises(BarcodeError):
        render_barcode("not-a-number", symbology="ean13", width_px=400, height_px=40)


def test_an_unknown_symbology_is_refused_by_name():
    with pytest.raises(BarcodeError, match="unsupported symbology"):
        render_barcode("42", symbology="qr", width_px=400, height_px=40)


def test_an_empty_payload_is_refused():
    with pytest.raises(BarcodeError):
        render_barcode("", width_px=400, height_px=40)


@pytest.mark.parametrize(
    ("symbology", "payload"),
    [
        ("code128", "SPOOL-42"),
        ("code39", "SPOOL42"),
        ("itf", "004212"),
        ("ean13", "590123412345"),
        ("ean8", "1234567"),
        ("upca", "01234567890"),
    ],
)
def test_every_symbology_offered_actually_renders(symbology, payload):
    """The editor will show this list, so an entry that does not render is a
    choice the operator can make and then not understand.
    """
    img = render_barcode(payload, symbology=symbology, width_px=600, height_px=40)
    assert img.mode == "1"
    assert _bar_runs(img), f"{symbology} drew no bars"


def test_the_offered_list_and_the_tested_list_are_the_same():
    """Otherwise a symbology can be added to the menu and never exercised."""
    tested = {"code128", "code39", "itf", "ean13", "ean8", "upca"}
    assert set(SUPPORTED) == tested
