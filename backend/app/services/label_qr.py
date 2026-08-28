"""QR codes for label rasters.

Split out from the renderers so both backends build a code the same way. The
1D symbologies live next door in ``label_barcode``; the two are separate because
a QR needs none of the module-width and quiet-zone arithmetic a barcode does —
the encoder handles its own quiet zone and the code is square by definition.
"""

from __future__ import annotations

import qrcode
from PIL import Image

#: A QR module has to be at least this many dots across to survive a
#: low-resolution thermal head. The same floor #1870 established for the PDF
#: templates, expressed in dots rather than millimetres because that is the unit
#: the constraint lives in.
MIN_MODULE_PX = 3
QUIET_ZONE_MODULES = 2


def render_qr(payload: str, side_px: int) -> Image.Image:
    """A square, bilevel QR of ``side_px`` on a side.

    ⚠️ Resized with NEAREST when the encoder's natural size does not match.
    Any smoothing reintroduces grey, and a blurred module edge is precisely
    what fails to scan.
    """
    qr = qrcode.QRCode(
        # ERROR_CORRECT_L for the reason label_renderer gives: fewer, chunkier
        # modules, which is what makes a code survive a 203 dpi head. A label
        # does not need damage-level recovery.
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=MIN_MODULE_PX,
        border=QUIET_ZONE_MODULES,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image().convert("1")
    if img.width != side_px:
        img = img.resize((max(1, side_px), max(1, side_px)), Image.NEAREST)
    return img


__all__ = ["MIN_MODULE_PX", "QUIET_ZONE_MODULES", "render_qr"]
