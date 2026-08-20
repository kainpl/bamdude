"""PDF spool label rendering (B.1 — port of upstream Bambuddy #809).

Six fixed templates:

- ``ams_holder_74x33`` — 74×33 mm single label, matches the printable
  label STL bundled with the Makerworld AMS Filament Label Holder
  (model 752566). Smaller variant — the visible window in the holder.
  One label per page.
- ``ams_holder_75x55`` — 75×55 mm single label, fits the cardstock-
  insert variant of the same holder. Roomier — swatch + QR + full
  text column. One label per page.
- ``box_40x30``  — 40×30 mm single label, common DK/Brother roll size and a
  good fit for filament-bag/storage-bin labels (#809 follow-up). Roomy
  layout — swatch, QR, full text column with hex code.
- ``box_62x29``  — 62×29 mm single label, sized for Brother PT/QL and Dymo
  generic small labels. One label per page.
- ``avery_5160`` — US Letter sheet, 25.4×66.7 mm × 30 per sheet.
- ``avery_l7160`` — A4 sheet, 38.1×63.5 mm × 21 per sheet.

The legacy ``ams_30x15`` preset (#809) was incorrect — the original
30×15 mm dimension didn't fit any documented variant of model 752566.
Replaced by the two ``ams_holder_*`` presets above (upstream Bambuddy
#1426 / commit 1677efb2).

The renderer is decoupled from the Spool model: callers build a ``LabelData``
list from whatever source (local DB, Spoolman, future) so the same code path
works in both modes.

The "name" field in ``LabelData`` is the bold central display line. BamDude
exposes a user-configurable spool naming template
(``settings.spool_display_template`` resolved through
``frontend/src/utils/spoolName.ts::formatSpoolDisplayName``); the route layer
forwards the pre-composed value here so the label name matches what the
operator sees on the Inventory page. When no override is supplied (e.g.
Spoolman path), the route falls back to ``color_name → slicer_filament_name
→ "{brand} {material}"``. Everything else (brand / material / subtype /
spool ID / storage location) renders as separate fixed text rows on the
label regardless of the naming template — those are layout decisions, not
naming-template ones.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import qrcode
from reportlab.lib.colors import Color, HexColor, black, white
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth as c_stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

from backend.app.services.label_canvas import draw_template
from backend.app.services.label_template import LabelSheetSpec, LabelTemplateSpec

logger = logging.getLogger(__name__)


# ── Fonts ────────────────────────────────────────────────────────────────────
#
# reportlab's built-in Type-1 faces are WinAnsi-encoded. Handed a character they
# cannot encode they do NOT raise — they switch to ZapfDingbats, whose ``n``
# glyph is a filled black square. So a spool named "Чорний матовий" printed as
# ``■■■■■■`` on every template, while Latin names were fine, which is exactly
# why it went unnoticed. ``uk`` is one of two first-class locales.
#
# Arimo (SIL OFL) replaces them: Latin + Cyrillic + Greek, and metric-compatible
# with Arial and therefore Helvetica — measured at ±0.1% across regular, bold
# and italic. That matters beyond taste: the layout below truncates on
# ``stringWidth``, so a wider face would silently start clipping names that fit
# today. Provenance and the rejected alternative: ``data/fonts/README.md``.
_FONT_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"
_FONT_FILES: dict[str, Path] = {
    "Arimo": _FONT_DIR / "Arimo-Regular.ttf",
    "Arimo-Bold": _FONT_DIR / "Arimo-Bold.ttf",
    "Arimo-Italic": _FONT_DIR / "Arimo-Italic.ttf",
}


def _register_label_fonts() -> tuple[str, str, str]:
    """Register the shipped TTFs, or fall back to the built-in faces.

    Degrading rather than raising keeps the promise the rest of this module
    makes — a label should always print, and a missing font file is not a reason
    to fail an inventory action. The fallback is loud in the log and pinned by a
    test, because a *silent* fallback is precisely how the Cyrillic bug hid.
    """
    try:
        for name, path in _FONT_FILES.items():
            pdfmetrics.registerFont(TTFont(name, str(path)))
    except Exception:
        logger.error(
            "Label fonts could not be registered from %s — falling back to the built-in "
            "faces. Non-Latin text on labels will print as filled squares.",
            _FONT_DIR,
            exc_info=True,
        )
        return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"

    pdfmetrics.registerFontFamily("Arimo", normal="Arimo", bold="Arimo-Bold", italic="Arimo-Italic")
    return "Arimo", "Arimo-Bold", "Arimo-Italic"


_FONT_REGULAR, _FONT_BOLD, _FONT_ITALIC = _register_label_fonts()

TemplateName = Literal[
    "ams_holder_74x33",
    "ams_holder_75x55",
    "box_40x30",
    "box_62x29",
    "avery_5160",
    "avery_l7160",
]


@dataclass
class LabelData:
    """Per-spool data needed to render a label.

    Decoupled from the SQLAlchemy model so the same renderer serves the local
    inventory and the Spoolman-backed inventory.
    """

    spool_id: int
    name: str
    material: str
    brand: str | None = None
    subtype: str | None = None
    rgba: str | None = None  # "RRGGBB" or "RRGGBBAA"; None → neutral grey
    extra_colors: list[str] | None = None  # additional hex colours (no '#')
    storage_location: str | None = None
    deeplink_url: str = ""  # what the QR encodes; caller composes it


# ── Colour helpers ───────────────────────────────────────────────────────────


def _color_from_hex(hex_str: str | None, fallback: Color = HexColor(0x808080)) -> Color:
    """Parse an RRGGBB or RRGGBBAA string (no '#') into a ReportLab Color.

    Alpha is honoured so multi-colour spools with translucent overlays render
    correctly. Falls back to ``fallback`` for None / malformed input rather
    than raising — labels should always print.
    """
    if not hex_str:
        return fallback
    h = hex_str.lstrip("#").strip()
    if len(h) not in (6, 8):
        return fallback
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
        a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
        return Color(r, g, b, alpha=a)
    except ValueError:
        return fallback


def _luminance(color: Color) -> float:
    """Perceived luminance of a ReportLab Color (0–1, WCAG-style approximation)."""
    return 0.299 * color.red + 0.587 * color.green + 0.114 * color.blue


def _hex_code_label(rgba: str | None) -> str:
    """Format ``data.rgba`` as a printable ``#RRGGBB`` string for the label.

    Drops the alpha channel (printed labels can't show transparency) and
    upper-cases the hex digits to match the colour-picker convention used in
    the inventory UI. Returns an empty string for None / malformed input so
    the caller can ``if hex_code:`` skip drawing without an exception.
    """
    if not rgba:
        return ""
    h = rgba.lstrip("#").strip()
    if len(h) not in (6, 8):
        return ""
    rgb = h[:6]
    if not all(c in "0123456789abcdefABCDEF" for c in rgb):
        return ""
    return f"#{rgb.upper()}"


# ── QR generation ────────────────────────────────────────────────────────────


def _qr_png_bytes(payload: str, *, box_size: int = 4, border: int = 2) -> bytes:
    """Render ``payload`` as a tight QR PNG. Empty payload returns empty bytes
    so callers can skip drawing without checking ahead of time.
    """
    if not payload:
        return b""
    qr = qrcode.QRCode(
        version=None,
        # ERROR_CORRECT_L (7% recovery) rather than M (15%): a label QR only
        # needs to survive being scanned off clean stock, not physical damage,
        # and L encodes the same payload in a lower version (fewer, chunkier
        # modules). That extra module size is what makes the code printable on
        # low-resolution 203 dpi thermal printers, where M-level density bled
        # the modules together on small labels (#1870).
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Single-label drawing ─────────────────────────────────────────────────────


def _draw_swatch(c: rl_canvas.Canvas, x: float, y: float, w: float, h: float, data: LabelData) -> None:
    """Draw the colour swatch. Multi-colour spools use vertical stripes
    (matching the FilamentSwatch convention in the frontend)."""
    primary = _color_from_hex(data.rgba)
    extras = [_color_from_hex(h) for h in (data.extra_colors or []) if h]
    colors = [primary, *extras]

    if not colors:
        c.setFillColor(HexColor(0x808080))
        c.rect(x, y, w, h, stroke=0, fill=1)
        return

    stripe_w = w / len(colors)
    for i, col in enumerate(colors):
        c.setFillColor(col)
        c.rect(x + i * stripe_w, y, stripe_w, h, stroke=0, fill=1)

    # Thin black border so light-colour swatches stay visible on white labels.
    c.setStrokeColor(black)
    c.setLineWidth(0.3)
    c.rect(x, y, w, h, stroke=1, fill=0)


def _roomy_qr_size(inner_w: float, inner_h: float) -> float:
    """QR edge length (points) for the roomy layout.

    Historically a flat 20% of inner width, which on the narrowest label
    (box_40x30, ~37.6 mm inner) rendered a ~7.5 mm QR — at 203 dpi each module
    fell below ~2 dots and the code bled into itself on thermal printers
    (#1870). A 12 mm floor keeps small labels scannable; the code is still
    capped by the inner height, an 18 mm absolute max, and ~45% of inner width
    so it can't crowd out the text column on an ultra-narrow label.
    """
    return min(max(inner_w * 0.20, 12 * mm), inner_h, 18 * mm, inner_w * 0.45)


def _draw_qr(c: rl_canvas.Canvas, x: float, y: float, size: float, payload: str) -> None:
    """Embed a square QR at (x, y) with edge length ``size`` (in points)."""
    png = _qr_png_bytes(payload)
    if not png:
        return
    from reportlab.lib.utils import ImageReader

    img = ImageReader(io.BytesIO(png))
    c.drawImage(img, x, y, width=size, height=size, mask="auto")


def _truncate_to_width(c: rl_canvas.Canvas, text: str, font: str, size: float, max_w: float) -> str:
    """Truncate ``text`` with an ellipsis so it fits within ``max_w`` points."""
    if c.stringWidth(text, font, size) <= max_w:
        return text
    ell = "…"
    while text and c.stringWidth(text + ell, font, size) > max_w:
        text = text[:-1]
    return text + ell if text else ell


def _draw_label(
    c: rl_canvas.Canvas, x: float, y: float, w: float, h: float, data: LabelData, monochrome: bool = False
) -> None:
    """Render one label inside the box (x, y, w, h). Origin is bottom-left.

    Two layouts, picked by available height:

    - **Tight** (h < 20 mm — AMS holder): swatch on the left, three lines of
      text on the right (brand, material+subtype, big spool ID). No QR — at
      30×15 mm there is not enough horizontal room for swatch + text + QR
      without truncating away the user-need fields, and the AMS holder is an
      at-a-glance identifier where the spool ID is the killer field. The
      box-label and Avery templates carry the QR for the other use cases.

    - **Roomy** (h >= 20 mm — box label, Avery sheets): swatch on the left,
      QR on the right, multi-line text in the middle column. Large spool ID
      anchored at bottom-left under the swatch so it stays readable when the
      label is on a box on a shelf at arm's length.
    """
    pad = 1.2 * mm
    inner_x, inner_y = x + pad, y + pad
    inner_w = w - 2 * pad
    inner_h = h - 2 * pad

    # Outer hairline border so labels are easy to cut out from blank stock.
    c.setStrokeColor(HexColor(0xCCCCCC))
    c.setLineWidth(0.4)
    c.rect(x, y, w, h, stroke=1, fill=0)

    is_tight = h < 20 * mm

    if is_tight:
        _draw_label_tight(c, x, y, w, h, inner_x, inner_y, inner_w, inner_h, pad, data, monochrome)
    else:
        _draw_label_roomy(c, x, y, w, h, inner_x, inner_y, inner_w, inner_h, pad, data, monochrome)


def _draw_label_tight(
    c: rl_canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    inner_x: float,
    inner_y: float,
    inner_w: float,
    inner_h: float,
    pad: float,
    data: LabelData,
    monochrome: bool = False,
) -> None:
    """AMS-holder layout (e.g. 30×15 mm). Swatch + brand/material/hex/ID, no QR."""
    # Monochrome: drop the colour swatch (see _draw_label_roomy) and give the
    # width to the text column (#1870).
    if monochrome:
        swatch_w = 0.0
    else:
        swatch_w = min(inner_h, inner_w * 0.35)
        swatch_y = inner_y + (inner_h - swatch_w) / 2
        _draw_swatch(c, inner_x, swatch_y, swatch_w, swatch_w, data)

    text_x = inner_x + swatch_w + pad
    text_w = inner_w - swatch_w - pad
    if text_w < 5 * mm:
        return  # Pathological — even the swatch barely fits.

    c.setFillColor(black)

    # Top: brand — bumped to bold + larger per the #809 follow-up so it's the
    # easiest thing to read on a small AMS holder at arm's length.
    brand_size = 6.5
    if data.brand:
        c.setFont(_FONT_BOLD, brand_size)
        brand = _truncate_to_width(c, data.brand, _FONT_BOLD, brand_size, text_w)
        c.drawString(text_x, y + h - pad - brand_size, brand)

    # Second line: material + subtype, small
    sub_size = 5
    sub_line = " ".join(filter(None, [data.material, data.subtype]))
    sub_y_baseline = y + h - pad - brand_size - 0.6 - sub_size
    if sub_line:
        c.setFont(_FONT_REGULAR, sub_size)
        sub_line = _truncate_to_width(c, sub_line, _FONT_REGULAR, sub_size, text_w)
        c.drawString(text_x, sub_y_baseline, sub_line)

    # Third line (when there's room): hex code, tiny — useful when the user
    # has multiple near-identical colours in the same material family.
    hex_code = _hex_code_label(data.rgba)
    if hex_code:
        hex_size = 4.5
        hex_y = sub_y_baseline - 0.4 - hex_size
        # Don't render if it'd collide with the spool ID at the bottom.
        if hex_y > inner_y + 13:
            c.setFont(_FONT_REGULAR, hex_size)
            c.drawString(text_x, hex_y, hex_code)

    # Bottom: BIG spool ID — the killer field at-a-glance.
    id_size = 13
    c.setFont(_FONT_BOLD, id_size)
    id_text = _truncate_to_width(c, f"#{data.spool_id}", _FONT_BOLD, id_size, text_w)
    c.drawString(text_x, inner_y + 0.5, id_text)


def _draw_label_roomy(
    c: rl_canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    inner_x: float,
    inner_y: float,
    inner_w: float,
    inner_h: float,
    pad: float,
    data: LabelData,
    monochrome: bool = False,
) -> None:
    """Box-label / Avery layout. Swatch left, QR right, text middle."""
    # Swatch: full inner height, ~18% of inner width but capped so we never
    # eat the text column on extreme aspect ratios. Omitted entirely in
    # monochrome mode — on a B&W thermal printer a colour block prints as a
    # muddy grey that conveys nothing, so we reclaim the space for text and
    # rely on the hex-code line to carry the colour (#1870). The hex code
    # already renders below whenever rgba is set.
    if monochrome:
        swatch_w = 0.0
    else:
        swatch_w = min(inner_w * 0.18, inner_h, 16 * mm)
        _draw_swatch(c, inner_x, inner_y, swatch_w, inner_h, data)

    qr_size = _roomy_qr_size(inner_w, inner_h)
    qr_x = x + w - pad - qr_size
    qr_y = inner_y + (inner_h - qr_size) / 2
    _draw_qr(c, qr_x, qr_y, qr_size, data.deeplink_url)

    text_x = inner_x + swatch_w + 1.5 * mm
    text_w = qr_x - text_x - 1.5 * mm
    if text_w < 8 * mm:
        return

    c.setFillColor(black)

    # Build the text rows we want to render, in top→bottom order.
    line1 = data.brand or ""
    line2 = " · ".join(filter(None, [data.material, data.subtype]))
    name = data.name or ""
    hex_code = _hex_code_label(data.rgba)

    # Layout from the top of the text column.
    cursor_y = y + h - pad

    # Brand — bumped to bold + larger per the #809 follow-up.
    if line1:
        size = 8
        c.setFont(_FONT_BOLD, size)
        text = _truncate_to_width(c, line1, _FONT_BOLD, size, text_w)
        cursor_y -= size
        c.drawString(text_x, cursor_y, text)
        cursor_y -= 1.2

    if line2:
        size = 7
        c.setFont(_FONT_REGULAR, size)
        text = _truncate_to_width(c, line2, _FONT_REGULAR, size, text_w)
        cursor_y -= size
        c.drawString(text_x, cursor_y, text)
        cursor_y -= 1.5

    # Hex colour code — useful for telling near-identical material+colour
    # spools apart when the swatch is small or the user is colour-blind.
    if hex_code:
        size = 6.5
        c.setFont(_FONT_REGULAR, size)
        cursor_y -= size
        c.drawString(text_x, cursor_y, hex_code)
        cursor_y -= 1.2

    if name and name != line1:
        size = 9
        c.setFont(_FONT_BOLD, size)
        text = _truncate_to_width(c, name, _FONT_BOLD, size, text_w)
        cursor_y -= size
        c.drawString(text_x, cursor_y, text)
        cursor_y -= 1.2

    if data.storage_location:
        size = 6.5
        c.setFont(_FONT_ITALIC, size)
        text = _truncate_to_width(c, data.storage_location, _FONT_ITALIC, size, text_w)
        cursor_y -= size
        c.drawString(text_x, cursor_y, text)

    # Spool ID — anchored at the bottom of the text column, big and bold.
    id_size = 16
    c.setFont(_FONT_BOLD, id_size)
    id_text = _truncate_to_width(c, f"#{data.spool_id}", _FONT_BOLD, id_size, text_w)
    c.drawString(text_x, inner_y + 0.5, id_text)


# ── Template entry points ────────────────────────────────────────────────────

# (label_w_mm, label_h_mm) for single-label-per-page templates.
_SINGLE_LABEL_SIZES_MM: dict[str, tuple[float, float]] = {
    "ams_holder_74x33": (74.0, 33.0),
    "ams_holder_75x55": (75.0, 55.0),
    "box_40x30": (40.0, 30.0),
    "box_62x29": (62.0, 29.0),
}

# Sheet template parameters: (page_size, label_w_mm, label_h_mm,
#                              cols, rows, top_margin_mm, left_margin_mm,
#                              col_gap_mm, row_gap_mm)
_SHEET_TEMPLATES: dict[str, tuple] = {
    "avery_5160": (letter, 66.675, 25.4, 3, 10, 12.7, 4.76, 3.175, 0.0),
    "avery_l7160": (A4, 63.5, 38.1, 3, 7, 15.15, 7.0, 2.5, 0.0),
}


def _render_single_label_pdf(template: TemplateName, data_list: list[LabelData], monochrome: bool = False) -> bytes:
    w_mm, h_mm = _SINGLE_LABEL_SIZES_MM[template]
    page_w, page_h = w_mm * mm, h_mm * mm

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(f"BamDude spool labels ({template})")

    for data in data_list:
        _draw_label(c, 0, 0, page_w, page_h, data, monochrome)
        c.showPage()

    c.save()
    return buf.getvalue()


def _render_sheet_pdf(template: TemplateName, data_list: list[LabelData], monochrome: bool = False) -> bytes:
    page_size, w_mm, h_mm, cols, rows, top_mm, left_mm, col_gap_mm, row_gap_mm = _SHEET_TEMPLATES[template]
    page_w, page_h = page_size

    label_w = w_mm * mm
    label_h = h_mm * mm
    top_margin = top_mm * mm
    left_margin = left_mm * mm
    col_gap = col_gap_mm * mm
    row_gap = row_gap_mm * mm

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=page_size)
    c.setTitle(f"BamDude spool labels ({template})")

    per_page = cols * rows
    for page_start in range(0, len(data_list), per_page):
        chunk = data_list[page_start : page_start + per_page]
        for idx, data in enumerate(chunk):
            row = idx // cols
            col = idx % cols
            x = left_margin + col * (label_w + col_gap)
            y = page_h - top_margin - (row + 1) * label_h - row * row_gap
            _draw_label(c, x, y, label_w, label_h, data, monochrome)
        c.showPage()

    c.save()
    return buf.getvalue()


def render_labels(template: TemplateName, data_list: list[LabelData], *, monochrome: bool = False) -> bytes:
    """Render ``data_list`` to a PDF using the named template. Returns bytes.

    Empty ``data_list`` still produces a valid (empty) PDF — callers should
    short-circuit beforehand if that's not desired.

    ``monochrome`` drops the colour swatch (which prints as a useless grey block
    on black-and-white thermal printers) and reclaims the space for text; the
    hex-code line still carries the colour. See #1870.
    """
    if template in _SINGLE_LABEL_SIZES_MM:
        return _render_single_label_pdf(template, data_list, monochrome)
    if template in _SHEET_TEMPLATES:
        return _render_sheet_pdf(template, data_list, monochrome)
    raise ValueError(f"Unknown label template: {template!r}")


__all__ = [
    "CODE_DOTS_PER_MM",
    "LabelData",
    "PdfCanvas",
    "TemplateName",
    "render_labels",
    "render_template_pdf",
    "render_template_sheet_pdf",
]

# ── Template-driven rendering ────────────────────────────────────────────────

#: Resolution the code images are built at before being placed in the PDF.
#:
#: ⚠️ Barcodes and QR codes go into the PDF as raster, not as reportlab's vector
#: symbologies. Two implementations would let one label differ by how it was
#: printed, which is exactly what having one template is meant to end. 600 dpi
#: is finer than any printer this reaches and invisible once placed.
CODE_DOTS_PER_MM = 600 / 25.4

#: Below this a glyph stops being type and becomes a smudge on a thermal head.
_MIN_TEXT_PT = 2.8


class PdfCanvas:
    """A [`label_canvas.LabelCanvas`] that draws one label onto a PDF page.

    ⚠️ **reportlab counts from the bottom-left of the page; a template counts
    from the top-left of the label.** The flip happens here, once. Anywhere else
    and every label comes out mirrored vertically, which reads as a layout bug
    rather than as a coordinate one.
    """

    def __init__(
        self,
        canvas: rl_canvas.Canvas,
        *,
        origin_mm: tuple[float, float],
        page_height_pt: float,
    ) -> None:
        self._c = canvas
        self._origin_x_mm, self._origin_y_mm = origin_mm
        self._page_height_pt = page_height_pt

    @property
    def dots_per_mm(self) -> float:
        return CODE_DOTS_PER_MM

    def box_to_points(self, box_mm) -> tuple[float, float, float, float]:
        """Box in millimetres from the label's top-left → points from the page's
        bottom-left, returning the box's own bottom-left corner.

        Public because it is the one piece of arithmetic in this class that can
        be wrong in a way nothing else notices, and a compressed PDF is no place
        to go looking for it.
        """
        x, y, w, h = box_mm
        left = (self._origin_x_mm + x) * mm
        top = (self._origin_y_mm + y) * mm
        bottom = self._page_height_pt - top - h * mm
        return left, bottom, w * mm, h * mm

    def text(
        self,
        text: str,
        *,
        box_mm,
        size_mm: float,
        bold: bool,
        italic: bool,
        align: str,
        valign: str,
        fit: str,
    ) -> None:
        left, bottom, box_w, box_h = self.box_to_points(box_mm)
        font = _FONT_BOLD if bold else (_FONT_ITALIC if italic else _FONT_REGULAR)
        size = max(_MIN_TEXT_PT, size_mm * mm)

        if fit == "shrink":
            while size > _MIN_TEXT_PT and (c_stringWidth(text, font, size) > box_w or size > box_h):
                size -= 0.25
        else:
            size = max(_MIN_TEXT_PT, min(size, box_h))

        # Truncation applies to both fits: it is what keeps text inside its box
        # rather than running over whatever sits beside it.
        drawn = _truncate_to_width(self._c, text, font, size, box_w)
        if not drawn:
            return

        width = c_stringWidth(drawn, font, size)
        x = {"left": left, "center": left + (box_w - width) / 2, "right": left + box_w - width}.get(align, left)
        # The baseline sits an ascender below the top of the box, so that `top`
        # means the same thing here as it does on the raster.
        ascent = size * 0.8
        top_of_box = bottom + box_h
        y = {
            "top": top_of_box - ascent,
            "middle": bottom + (box_h - size) / 2 + (size - ascent),
            "bottom": bottom + (size - ascent),
        }.get(valign, top_of_box - ascent)

        self._c.setFont(font, size)
        self._c.drawString(x, y, drawn)

    def image(self, img, *, box_mm) -> None:
        left, bottom, box_w, box_h = self.box_to_points(box_mm)
        # Keep the code's own aspect ratio and centre it, the way the raster
        # backend does — a stretched barcode is an unreadable one.
        scale = min(box_w / img.width, box_h / img.height)
        width, height = img.width * scale, img.height * scale
        self._c.drawImage(
            ImageReader(img.convert("L")),
            left + (box_w - width) / 2,
            bottom + (box_h - height) / 2,
            width=width,
            height=height,
        )


def _page_size(name: str) -> tuple[float, float]:
    return A4 if name == "A4" else letter


def render_template_pdf(spec: LabelTemplateSpec, contexts: list[dict[str, str]]) -> tuple[bytes, list[str]]:
    """One page per spool, each page the size of the label.

    ⚠️ Warnings are collected once, not once per spool. The same template drawn
    twenty times has one fault, and a list that grows with the batch buries
    whatever else is in it.
    """
    page = (spec.width_mm * mm, spec.height_mm * mm)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=page)
    c.setTitle(f"BamDude label ({spec.name})")

    warnings: list[str] = []
    for context in contexts:
        canvas = PdfCanvas(c, origin_mm=(0.0, 0.0), page_height_pt=page[1])
        found = draw_template(canvas, spec, context)
        for warning in found:
            if warning not in warnings:
                warnings.append(warning)
        c.showPage()

    c.save()
    return buf.getvalue(), warnings


def render_template_sheet_pdf(
    spec: LabelTemplateSpec, contexts: list[dict[str, str]], sheet: LabelSheetSpec
) -> tuple[bytes, list[str]]:
    """The same label repeated across a page of stock."""
    page_w, page_h = _page_size(sheet.page_size)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(f"BamDude labels ({spec.name} on {sheet.name})")

    warnings: list[str] = []
    per_page = sheet.per_page
    for start in range(0, max(len(contexts), 1), per_page):
        chunk = contexts[start : start + per_page]
        for index, context in enumerate(chunk):
            row, col = divmod(index, sheet.cols)
            origin = (
                sheet.margin_left_mm + col * (sheet.cell_width_mm + sheet.gap_x_mm),
                sheet.margin_top_mm + row * (sheet.cell_height_mm + sheet.gap_y_mm),
            )
            canvas = PdfCanvas(c, origin_mm=origin, page_height_pt=page_h)
            for warning in draw_template(canvas, spec, context):
                if warning not in warnings:
                    warnings.append(warning)
        c.showPage()

    c.save()
    return buf.getvalue(), warnings


# white re-exported for completeness; future templates may need a paper-tone variant.
_ = white
# _luminance is exported for future templates that need contrast-adaptive text.
_ = _luminance
