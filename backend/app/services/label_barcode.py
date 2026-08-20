"""1D barcodes for label rasters.

``python-barcode`` does the encoding — the symbology tables, the check digits,
the start and stop patterns — and hands back a string of modules, ``1`` for a
bar and ``0`` for a space. This module draws that string.

⚠️ **The drawing is ours on purpose, and it is the whole reason this file
exists.** The library's own image writer walks a floating-point cursor and
rounds each bar independently, so asking it for a four-pixel module yields bars
of three, four, eight and twelve pixels — a ±25 % wobble on the narrow element.
A scanner reads the *ratios* between bars, so that wobble is precisely what it
cannot tolerate. Measured rather than assumed: an earlier version of this file
used the writer, and its test passed only because the payload it happened to
use came out even.

The same reasoning rules out drawing at a convenient size and scaling the result
into place. Resampling a bilevel image by a fractional factor rounds every edge
independently and destroys the ratios the same way. So the barcode is drawn to
the box it will occupy, at a whole number of pixels per module, and never
resampled.

Physical size needs no thought here. The box comes from the label template in
millimetres, so a 30 mm box is 30 mm on any printer, whatever its resolution.
"""

from __future__ import annotations

import logging

import barcode
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

#: Symbologies offered on labels. The library provides more (ISBN, ISSN, PZN and
#: friends), but a spool label has no use for a book number, and every entry
#: here is one more thing to explain in the editor.
SUPPORTED = ("code128", "code39", "ean13", "ean8", "upca", "itf")

#: Narrowest bar worth printing, in pixels. Below two, a thermal head merges
#: adjacent bars and the scanner sees one wide one.
MIN_MODULE_PX = 2

#: Blank margin either side, in modules. A barcode without one is a barcode the
#: scanner cannot find the start of; ten is the usual minimum for Code 128.
QUIET_ZONE_MODULES = 10


class BarcodeError(ValueError):
    """The payload cannot be encoded in the requested symbology.

    Raised rather than swallowed: a fixed-length symbology given the wrong
    number of digits (EAN-13 wants 12 or 13) would otherwise reach the printer
    as a blank space exactly where a barcode was meant to be.
    """


def modules_for(payload: str, symbology: str = "code128") -> str:
    """The bar/space pattern, straight from the library. ``1`` is a bar."""
    kind = symbology.strip().lower()
    if kind not in SUPPORTED:
        raise BarcodeError(f"unsupported symbology {symbology!r}; expected one of {SUPPORTED}")
    if not payload:
        raise BarcodeError("nothing to encode")
    try:
        built = barcode.get(kind, payload).build()
    except Exception as error:  # noqa: BLE001 - the library raises several bare types
        raise BarcodeError(f"{kind}: {error}") from error
    if not built or not built[0]:
        raise BarcodeError(f"{kind}: encoded to nothing")
    return built[0]


def render_barcode(
    payload: str,
    *,
    symbology: str = "code128",
    width_px: int,
    height_px: int,
) -> Image.Image:
    """Draw a barcode filling ``width_px`` × ``height_px`` as closely as whole
    modules allow, centred in that box.

    ⚠️ **Code 128 is not compact.** ``SPOOL-42`` is 123 modules, plus twenty of
    quiet zone; at the two-pixel floor that is 286 px, which is most of a 40 mm
    label at 203 dpi. Short payloads are not a style preference here — a spool
    id encodes comfortably where a spool name does not.
    """
    modules = modules_for(payload, symbology)
    total = len(modules) + 2 * QUIET_ZONE_MODULES

    module_px = max(MIN_MODULE_PX, width_px // total)
    drawn_width = module_px * total
    if drawn_width > width_px:
        logger.warning(
            "Barcode %r in %s needs %s px at the %s px module floor but has %s. It will "
            "overflow its box — drawing it narrower would fit and not scan.",
            payload,
            symbology,
            drawn_width,
            MIN_MODULE_PX,
            width_px,
        )

    img = Image.new("1", (max(drawn_width, width_px), max(1, height_px)), 1)
    draw = ImageDraw.Draw(img)

    # Start of the symbol itself: past the quiet zone, and centred in whatever
    # slack rounding to whole modules left behind.
    start = (img.width - drawn_width) // 2 + QUIET_ZONE_MODULES * module_px
    for index, module in enumerate(modules):
        if module == "1":
            left = start + index * module_px
            draw.rectangle([left, 0, left + module_px - 1, img.height - 1], fill=0)

    return img


__all__ = [
    "MIN_MODULE_PX",
    "QUIET_ZONE_MODULES",
    "SUPPORTED",
    "BarcodeError",
    "modules_for",
    "render_barcode",
]
