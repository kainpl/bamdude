"""A label as a colour picture: the PDF backend's own page, rasterised.

The preview in the print dialog and "save as PNG" both show what the printer
path draws — the same walk, colour block included — at the design's own size in
millimetres times the dots per inch asked for. Nothing is drawn a third way and
nothing is scaled.
"""

from __future__ import annotations

import asyncio
import io
import struct
import threading
import time
import zipfile

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.app.services import label_picture
from backend.app.services.label_context import spoolman_context
from backend.app.services.label_template import LabelTemplateSpec

CONTEXT = {
    "display_name": "Polymaker PLA Ivory",
    "material": "PLA",
    "color_hex_all": "FF0000",
    "deeplink": "https://bam.example/inventory?spool=42",
}

SWATCH = {"type": "swatch", "x_mm": 2, "y_mm": 2, "w_mm": 10, "h_mm": 10, "content": "{color_hex_all}"}
TEXT = {"type": "text", "x_mm": 14, "y_mm": 2, "w_mm": 24, "h_mm": 6, "content": "{display_name}", "size_mm": 3}
QR = {"type": "qr", "x_mm": 14, "y_mm": 10, "w_mm": 14, "h_mm": 14, "content": "{deeplink}"}


def _spec(*elements, width_mm=40.0, height_mm=30.0) -> LabelTemplateSpec:
    return LabelTemplateSpec(
        name="T", width_mm=width_mm, height_mm=height_mm, elements=list(elements or (SWATCH, TEXT, QR))
    )


def _open(png: bytes) -> Image.Image:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    return Image.open(io.BytesIO(png))


def _phys(png: bytes) -> tuple[int, int, int]:
    """The pHYs chunk as written: pixels per unit on x and y, and the unit (1 = metre)."""
    at = png.index(b"pHYs")
    return struct.unpack(">IIB", png[at + 4 : at + 13])


def test_the_colour_block_is_in_the_picture():
    """The audit's Д5: the 1-bit raster leaves a hole where a driver design has its swatch."""
    (png,), warnings = label_picture.render_pictures(_spec(), [CONTEXT], dpi=203)
    image = _open(png).convert("RGB")
    # The middle of the swatch box: (2 + 5) mm from the corner on both axes.
    at = round(7 * 203 / 25.4)
    assert image.getpixel((at, at)) == (255, 0, 0)
    assert image.getpixel((2, 2)) == (255, 255, 255)
    assert not warnings


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_the_picture_is_the_design_s_own_size(dpi):
    (png,), _ = label_picture.render_pictures(_spec(width_mm=74.0, height_mm=33.0), [CONTEXT], dpi=dpi)
    expected = (round(74.0 * dpi / 25.4), round(33.0 * dpi / 25.4))
    assert _open(png).size == expected == label_picture.picture_size(_spec(width_mm=74.0, height_mm=33.0), dpi)


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_the_png_says_its_resolution_so_it_prints_at_true_size(dpi):
    (png,), _ = label_picture.render_pictures(_spec(), [CONTEXT], dpi=dpi)
    per_metre = round(dpi / 0.0254)
    assert _phys(png) == (per_metre, per_metre, 1)


def test_one_picture_per_label_in_the_order_asked():
    red, blue = {**CONTEXT, "color_hex_all": "FF0000"}, {**CONTEXT, "color_hex_all": "0000FF"}
    pictures, _ = label_picture.render_pictures(_spec(), [red, blue, red], dpi=203)
    at = round(7 * 203 / 25.4)
    assert [_open(png).convert("RGB").getpixel((at, at)) for png in pictures] == [
        (255, 0, 0),
        (0, 0, 255),
        (255, 0, 0),
    ]


def test_a_warning_of_the_walk_comes_back_with_the_pictures():
    off_the_edge = {**TEXT, "x_mm": 30}
    _, warnings = label_picture.render_pictures(_spec(off_the_edge), [CONTEXT], dpi=203)
    assert any("extends past the label edge" in warning for warning in warnings)


def test_a_spoolman_spool_gets_the_same_picture():
    payload = {
        "id": 7,
        "filament": {"name": "PolyLite PLA Red", "material": "PLA", "color_hex": "#00FF00", "vendor": {"name": "PM"}},
    }
    context = spoolman_context(payload, deeplink_base="https://bam.example")
    (png,), _ = label_picture.render_pictures(_spec(), [context], dpi=203)
    at = round(7 * 203 / 25.4)
    assert _open(png).convert("RGB").getpixel((at, at)) == (0, 255, 0)


class TestBounds:
    def test_600_dpi_of_an_ordinary_label_is_a_picture(self):
        spec = _spec(width_mm=100.0, height_mm=50.0)
        label_picture.checked(spec, 600, labels=1)
        assert label_picture.picture_size(spec, 600) == (2362, 1181)

    def test_a_design_too_large_for_the_resolution_is_refused_in_words(self):
        with pytest.raises(HTTPException) as refused:
            label_picture.checked(_spec(width_mm=200.0, height_mm=50.0), 600, labels=1)
        assert refused.value.status_code == 422
        assert refused.value.detail == (
            "At 600 dpi this design is more than 4000 dots on a side — choose a lower resolution."
        )

    def test_the_same_design_is_fine_at_a_lower_resolution(self):
        label_picture.checked(_spec(width_mm=200.0, height_mm=50.0), 300, labels=1)

    def test_more_labels_than_one_export_takes_is_refused(self):
        with pytest.raises(HTTPException) as refused:
            label_picture.checked(_spec(), 203, labels=label_picture.MAX_PICTURES + 1)
        assert refused.value.status_code == 422
        assert str(label_picture.MAX_PICTURES) in refused.value.detail

    def test_the_renderer_itself_draws_nothing_past_the_bound(self, monkeypatch):
        """The gate is asked first by every door; a caller that forgot it still gets no wall-sized bitmap."""

        def reached(*_args, **_kwargs):
            raise AssertionError("the PDF backend was reached")

        monkeypatch.setattr(label_picture, "render_template_pdf", reached)
        with pytest.raises(ValueError):
            label_picture.render_pictures(_spec(width_mm=200.0, height_mm=50.0), [CONTEXT], dpi=600)

    async def test_the_front_door_refuses_before_any_render(self, monkeypatch):
        def reached(*_args, **_kwargs):
            raise AssertionError("the renderer was reached")

        monkeypatch.setattr(label_picture, "render_pictures", reached)
        with pytest.raises(HTTPException):
            await label_picture.label_pictures(_spec(width_mm=200.0, height_mm=50.0), [CONTEXT], dpi=600)


class TestOffTheLoop:
    async def test_the_loop_keeps_serving_while_pictures_are_drawn(self, monkeypatch):
        slow = 0.5
        real = label_picture.render_pictures
        drawn: list[int] = []

        def slow_render(*args, **kwargs):
            time.sleep(slow)
            drawn.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(label_picture, "render_pictures", slow_render)
        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            pictures, _ = await label_picture.label_pictures(_spec(), [CONTEXT], dpi=203)
        finally:
            done.set()
            await beat

        assert len(pictures) == 1
        assert drawn and drawn[0] != threading.get_ident()
        assert ticks >= int(slow / 0.02 / 2), ticks


class TestNames:
    def test_the_number_then_the_name(self):
        assert label_picture.picture_names([(42, "Polymaker PLA Ivory")]) == ["0042-polymaker-pla-ivory.png"]

    def test_a_name_is_made_safe_for_any_file_system(self):
        (name,) = label_picture.picture_names([(7, '../..\\Білий: PLA "Matte" / 1.75?')])
        assert name == "0007-білий-pla-matte-1-75.png"

    def test_a_spool_without_a_name_is_its_number(self):
        assert label_picture.picture_names([(7, "  ")]) == ["0007.png"]

    def test_a_repeated_label_is_numbered(self):
        assert label_picture.picture_names([(7, "PLA"), (8, "PLA"), (7, "PLA"), (7, "PLA")]) == [
            "0007-pla.png",
            "0008-pla.png",
            "0007-pla-2.png",
            "0007-pla-3.png",
        ]

    def test_a_long_name_is_cut(self):
        (name,) = label_picture.picture_names([(1, "x" * 500)])
        assert len(name) <= 80 and name.endswith(".png")


class TestTheBundle:
    def test_a_zip_of_the_pictures_under_their_names_in_order(self):
        archive = label_picture.bundle(["b.png", "a.png"], [b"second", b"first"])
        with zipfile.ZipFile(io.BytesIO(archive)) as opened:
            assert opened.namelist() == ["b.png", "a.png"]
            assert opened.read("b.png") == b"second"

    def test_the_same_pictures_give_the_same_bytes(self):
        assert label_picture.bundle(["a.png"], [b"x"]) == label_picture.bundle(["a.png"], [b"x"])

    def test_drawing_stops_at_the_byte_bound(self, monkeypatch):
        monkeypatch.setattr(label_picture, "MAX_BUNDLE_BYTES", 10)
        with pytest.raises(label_picture.BundleTooLarge):
            label_picture.render_pictures(_spec(), [CONTEXT, CONTEXT], dpi=203)

    def test_more_bytes_than_one_export_carries_is_refused(self, monkeypatch):
        monkeypatch.setattr(label_picture, "MAX_BUNDLE_BYTES", 10)
        with pytest.raises(label_picture.BundleTooLarge):
            label_picture.bundle(["a.png", "b.png"], [b"123456", b"123456"])


class TestWithoutThePdfRenderer:
    """An install whose requirements were not refreshed has no ``pypdfium2`` (review B-M9)."""

    async def test_the_door_answers_a_503_in_words(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "pypdfium2", None)  # ``import pypdfium2`` now raises ImportError

        with pytest.raises(HTTPException) as refused:
            await label_picture.label_pictures(_spec(), [CONTEXT], dpi=203)

        assert refused.value.status_code == 503
        assert refused.value.detail == (
            "The picture renderer (pypdfium2) is not installed on this server — "
            "reinstall the requirements, then try again"
        )

    async def test_the_sentence_has_its_ukrainian(self):
        import json
        from pathlib import Path

        import backend.app as app

        catalog = json.loads((Path(app.__file__).parent / "data" / "api_errors_uk.json").read_text(encoding="utf-8"))
        assert catalog[label_picture.RENDERER_MISSING_DETAIL]
