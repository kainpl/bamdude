"""1-bit label rasters for direct-to-device printing.

Deliberately **not** a second output mode of ``label_renderer.py``. That
renderer targets paper at PDF resolution with a colour swatch; this one targets
a thermal head a few centimetres wide, where the swatch is a grey smear, the QR
has to be coarse enough to survive one dot per module, and antialiasing is
damage rather than polish. The two share ``LabelData`` and nothing else — a
common canvas abstraction would drag both to the lowest common denominator.

Two properties of the label arrive as arguments rather than as constants, and
both for the same reason: they belong to the device, and the device tells us.

- **Size in millimetres** comes from the cassette in the machine. Cassette sizes
  are a long tail, so a fixed list of named templates would be wrong for the
  first size nobody thought of.
- **Dots per millimetre** comes from the printer's model. An earlier draft baked
  in 8 (203 dpi) on the grounds that every Niimbot is 203 dpi. That is false
  inside the set of models the bridge's first ported print flow already covers:
  an M2_H is 300 dpi.

⚠️ **The raster is the label, not the printhead.** Measured on a B1: 320 columns
sent to a 384-column head landed flush with the edges of a 40 mm label. The head
aligns to its edge, so a narrower image needs no padding — and rendering to the
head's full width instead puts the edges of the image past the edges of the
paper, which reads as "the corners are missing".
"""

from __future__ import annotations

import io
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

from backend.app.services.label_canvas import Box, draw_template
from backend.app.services.label_qr import render_qr
from backend.app.services.label_raster_fonts import font_at
from backend.app.services.label_renderer import LabelData
from backend.app.services.label_template import LabelTemplateSpec

Layout = Literal["roomy", "tight"]

# A QR module has to be at least this many dots across to survive a low-
# resolution thermal head — the same floor #1870 established for the PDF
# templates, expressed in dots rather than millimetres because that is the unit
# the constraint actually lives in.
_MIN_QR_MODULE_PX = 3
_QUIET_ZONE_MODULES = 2
# Version 3 at error-correction L is 29 modules, which holds a deep link.
_QR_MODULES = 29
_MARGIN_PX = 4
_MIN_FONT_PX = 9
# Deliberately generous. What should bound the name is the label — its width
# through truncation, its height through the row budget — not a number chosen
# here. A low cap left the bottom half of a 30 mm label empty while the name sat
# small enough to read at arm's length only if you already knew what it said.
_MAX_FONT_PX = 96


def _qr_side_px() -> int:
    return (_QR_MODULES + 2 * _QUIET_ZONE_MODULES) * _MIN_QR_MODULE_PX


def choose_layout(width_px: int, height_px: int) -> Layout:
    """``roomy`` when a scannable code still leaves a usable text column.

    Decided in pixels, not millimetres: what a QR needs is a number of dots, and
    the millimetres that come to depends on the printer.
    """
    side = _qr_side_px()
    fits_height = side + 2 * _MARGIN_PX <= height_px
    leaves_text = side + 2 * _MARGIN_PX < width_px * 0.6
    return "roomy" if fits_height and leaves_text else "tight"


def render_label_raster(
    data: LabelData,
    *,
    width_mm: float,
    height_mm: float,
    dots_per_mm: float,
    layout: Layout | None = None,
    rotate: int = 0,
) -> Image.Image:
    """One label, ready to hand to a printer. Mode ``"1"``, never greyscale."""
    if rotate not in (0, 90, 180, 270):
        raise ValueError(f"rotate must be one of 0/90/180/270, got {rotate!r}")

    width_px = _pad_to_byte(max(8, round(width_mm * dots_per_mm)))
    height_px = max(8, round(height_mm * dots_per_mm))
    chosen: Layout = layout or choose_layout(width_px, height_px)

    # Mode "1" from the start: ImageDraw does not antialias onto a bilevel
    # image, so there is never a grey pixel to threshold away afterwards.
    img = Image.new("1", (width_px, height_px), 1)
    draw = ImageDraw.Draw(img)

    text_right = width_px - _MARGIN_PX
    if chosen == "roomy" and data.deeplink_url:
        side = min(_qr_side_px(), height_px - 2 * _MARGIN_PX, width_px - 2 * _MARGIN_PX)
        if side >= _MIN_QR_MODULE_PX * (_QR_MODULES + 2 * _QUIET_ZONE_MODULES) // 2:
            code = _qr_image(data.deeplink_url, side)
            img.paste(code, (width_px - _MARGIN_PX - code.width, (height_px - code.height) // 2))
            text_right = width_px - _MARGIN_PX - code.width - _MARGIN_PX

    _draw_text_column(draw, data, _MARGIN_PX, text_right, height_px)

    if rotate:
        img = img.rotate(rotate, expand=True)
    return img


def render_label_png(
    data: LabelData,
    *,
    width_mm: float,
    height_mm: float,
    dots_per_mm: float,
    layout: Layout | None = None,
    rotate: int = 0,
) -> bytes:
    """The same label as PNG bytes, which is what travels to the device."""
    img = render_label_raster(
        data,
        width_mm=width_mm,
        height_mm=height_mm,
        dots_per_mm=dots_per_mm,
        layout=layout,
        rotate=rotate,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", bits=1)
    return buf.getvalue()


def _pad_to_byte(px: int) -> int:
    return px + (-px % 8)


def _qr_image(payload: str, side_px: int) -> Image.Image:
    """Kept as a name the fixed layouts below still call. The building moved to
    ``label_qr`` so the template walker and these share one QR."""
    return render_qr(payload, side_px)


def _draw_text_column(draw: ImageDraw.ImageDraw, data: LabelData, left: int, right: int, height_px: int) -> None:
    width = max(1, right - left)

    # How many rows there will be decides how tall each may be. Four is the most
    # this layout ever draws, so the name gets a quarter of the height and the
    # rest follow it down.
    wants_second = any((data.brand, data.material, data.subtype))
    row_count = 1 + int(wants_second) + int(bool(data.storage_location)) + 1
    ceiling = max(_MIN_FONT_PX, (height_px - 2 * _MARGIN_PX) // max(row_count, 1))

    rows: list[tuple[str, ImageFont.FreeTypeFont]] = []
    name_size = _fit_size(draw, data.name, width, ceiling=ceiling, bold=True)
    if data.name:
        rows.append((data.name, font_at(name_size, bold=True)))

    small = max(_MIN_FONT_PX, int(name_size * 0.62))
    if wants_second:
        rows.append((_compose_details(draw, data, font_at(small), width), font_at(small)))
    if data.storage_location:
        rows.append((data.storage_location, font_at(max(_MIN_FONT_PX, small - 1), italic=True)))
    rows.append((f"#{data.spool_id}", font_at(small, bold=True)))

    # Leading first, then centre the block vertically.
    #
    # ⚠️ Centred rather than top-aligned, and it is not decoration. The QR is
    # already centred on its side, so a text column starting at the top leaves
    # one band of white under it and reads as a label that failed to finish. The
    # width of the code caps how large the name can get, so on a roomy label
    # there is always slack — the only question is whether it looks deliberate.
    used = sum(font.size for _, font in rows)
    slack = max(0, height_px - 2 * _MARGIN_PX - used)
    gap = min(6, slack // max(len(rows) - 1, 1)) if len(rows) > 1 else 0

    block = used + gap * max(len(rows) - 1, 0)
    y = max(_MARGIN_PX, (height_px - block) // 2)
    for text, font in rows:
        if y + font.size > height_px - _MARGIN_PX:
            # Better a label missing its last line than one with a line sliced
            # in half by the edge of the paper.
            break
        draw.text((left, y), _truncate(draw, text, font, width), font=font, fill=0)
        y += font.size + gap


def _compose_details(draw: ImageDraw.ImageDraw, data: LabelData, font: ImageFont.FreeTypeFont, width: int) -> str:
    """Brand, material and subtype in whatever combination actually fits.

    ⚠️ Dropped whole rather than cut mid-string. Truncating the joined line by
    characters loses the tail, and the tail is the **material** — the one field
    on the label somebody is looking for when they pick a spool off the shelf.
    Losing the brand instead costs nothing they cannot see from the colour.
    """
    brand, material, subtype = data.brand, data.material, data.subtype
    for parts in (
        (brand, material, subtype),
        (brand, material),
        (material, subtype),
        (material,),
    ):
        line = " · ".join(part for part in parts if part)
        if line and draw.textlength(line, font=font) <= width:
            return line
    return material or ""


def _fit_size(draw: ImageDraw.ImageDraw, text: str, width: int, *, ceiling: int, bold: bool) -> int:
    size = max(_MIN_FONT_PX, min(ceiling, _MAX_FONT_PX))
    while size > _MIN_FONT_PX and draw.textlength(text, font=font_at(size, bold=bold)) > width:
        size -= 1
    return size


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    out = text
    while out and draw.textlength(out + "…", font=font) > width:
        out = out[:-1]
    return (out + "…") if out else ""


# ── Template-driven rendering ────────────────────────────────────────────────


class RasterCanvas:
    """A [`label_canvas.LabelCanvas`] that draws onto a 1-bit image.

    Bilevel from the start: ImageDraw does not antialias onto a mode-"1" image,
    so there is never a grey pixel to threshold away afterwards, and a grey edge
    is what a thermal head turns into a smudge.
    """

    def __init__(self, width_mm: float, height_mm: float, dots_per_mm: float) -> None:
        self._dots_per_mm = dots_per_mm
        width_px = _pad_to_byte(max(8, round(width_mm * dots_per_mm)))
        height_px = max(8, round(height_mm * dots_per_mm))
        self._img = Image.new("1", (width_px, height_px), 1)
        self._draw = ImageDraw.Draw(self._img)

    @property
    def dots_per_mm(self) -> float:
        return self._dots_per_mm

    def image_out(self) -> Image.Image:
        return self._img

    def _px(self, box_mm: Box) -> tuple[int, int, int, int]:
        x, y, w, h = box_mm
        return (
            round(x * self._dots_per_mm),
            round(y * self._dots_per_mm),
            max(1, round(w * self._dots_per_mm)),
            max(1, round(h * self._dots_per_mm)),
        )

    def text(
        self,
        text: str,
        *,
        box_mm: Box,
        size_mm: float,
        bold: bool,
        italic: bool,
        align: str,
        valign: str,
        fit: str,
    ) -> None:
        x, y, box_w, box_h = self._px(box_mm)
        size = max(_MIN_FONT_PX, round(size_mm * self._dots_per_mm))
        font = font_at(size, bold=bold, italic=italic)

        if fit == "shrink":
            while size > _MIN_FONT_PX and (self._draw.textlength(text, font=font) > box_w or size > box_h):
                size -= 1
                font = font_at(size, bold=bold, italic=italic)
        else:
            # `clip` keeps the authored size, but never taller than its box —
            # otherwise the line below it is what pays for the choice.
            size = max(_MIN_FONT_PX, min(size, box_h))
            font = font_at(size, bold=bold, italic=italic)

        # ⚠️ Truncation applies to both fits. It is what guarantees the text
        # cannot run past its box into whatever the operator put beside it.
        drawn = _truncate(self._draw, text, font, box_w)
        if not drawn:
            return

        width = self._draw.textlength(drawn, font=font)
        left = {
            "left": x,
            "center": x + (box_w - width) / 2,
            "right": x + box_w - width,
        }.get(align, x)
        top = {
            "top": y,
            "middle": y + (box_h - size) / 2,
            "bottom": y + box_h - size,
        }.get(valign, y)

        self._draw.text((left, top), drawn, font=font, fill=0)

    def swatch(self, colours: list[str], *, box_mm: Box) -> None:
        """Nothing, on purpose.

        ⚠️ A colour block on a one-bit head is a solid smear that conveys
        nothing — which is exactly why the PDF renderer already drops it in
        monochrome mode. Skipping it here rather than refusing the element means
        one template serves both a thermal printer and a colour sheet, with the
        hex-code line carrying the colour where the block cannot.
        """

    def image(self, img: Image.Image, *, box_mm: Box) -> None:
        x, y, box_w, box_h = self._px(box_mm)
        # Centred in its box. A barcode too wide for its box keeps its module
        # floor and overflows rather than shrinking into something unreadable,
        # so this can put ink outside the box on purpose — the walker warns.
        left = x + (box_w - img.width) // 2
        top = y + (box_h - img.height) // 2
        self._img.paste(img, (left, top))


def render_template_raster(
    spec: LabelTemplateSpec, context: dict[str, str], *, dots_per_mm: float
) -> tuple[Image.Image, list[str]]:
    """One label from one template, plus whatever went wrong drawing it."""
    canvas = RasterCanvas(spec.width_mm, spec.height_mm, dots_per_mm)
    warnings = draw_template(canvas, spec, context)
    return canvas.image_out(), warnings


def render_template_png(
    spec: LabelTemplateSpec, context: dict[str, str], *, dots_per_mm: float
) -> tuple[bytes, list[str]]:
    """The same label as PNG bytes, which is what travels to a device."""
    img, warnings = render_template_raster(spec, context, dots_per_mm=dots_per_mm)
    buf = io.BytesIO()
    img.save(buf, format="PNG", bits=1)
    return buf.getvalue(), warnings


__all__ = [
    "Layout",
    "RasterCanvas",
    "choose_layout",
    "render_label_png",
    "render_label_raster",
    "render_template_png",
    "render_template_raster",
]
