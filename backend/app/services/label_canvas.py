"""Walking a template, once, for whichever backend is drawing it.

The layout lives in the template; what differs between a 1-bit raster and a PDF
is only the drawing primitives. So this module holds the walk — resolve each
element's text, work out its box, decide what image a code element needs — and
the backends hold `text` and `image`.

⚠️ An earlier design rejected a shared drawing layer, on the grounds that the
two renderers each carried a hand-written layout that such a layer would have
had to reconcile. That objection died with the layouts: they are data now, so
there is nothing left to reconcile and only the primitives differ.
"""

from __future__ import annotations

import logging
from typing import Protocol

from PIL import Image

from backend.app.services.label_barcode import BarcodeError, render_barcode
from backend.app.services.label_qr import render_qr
from backend.app.services.label_template import LabelTemplateSpec, resolve

logger = logging.getLogger(__name__)

#: x, y, width, height — millimetres, from the top-left of the label.
Box = tuple[float, float, float, float]


class LabelCanvas(Protocol):
    """What a backend has to be able to do."""

    @property
    def dots_per_mm(self) -> float:
        """The resolution this canvas works at, so codes are built to match."""

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
    ) -> None: ...

    def image(self, img: Image.Image, *, box_mm: Box) -> None: ...

    def swatch(self, colours: list[str], *, box_mm: Box, shape: str = "rect") -> None:
        """Draw a block of colour. A 1-bit canvas implements this as nothing."""


def draw_template(canvas: LabelCanvas, spec: LabelTemplateSpec, context: dict[str, str]) -> list[str]:
    """Draw every element of ``spec`` onto ``canvas``. Returns what went wrong.

    ⚠️ **Nothing here raises.** A template with one bad element — a barcode
    whose payload will not encode, a box hanging off the edge — still produces a
    label, with the trouble described in the return value. The alternative is a
    print button that answers 500 because one field of one spool is empty, which
    is worse than a label with a gap in it and a sentence explaining the gap.
    """
    warnings: list[str] = []

    for index, element in enumerate(spec.elements):
        box: Box = (element.x_mm, element.y_mm, element.w_mm, element.h_mm)

        if (
            element.x_mm < 0
            or element.y_mm < 0
            or element.x_mm + element.w_mm > spec.width_mm + 1e-6
            or element.y_mm + element.h_mm > spec.height_mm + 1e-6
        ):
            warnings.append(f"element {index + 1} ({element.type}) extends past the label edge and will be clipped")

        content = resolve(element.content, context)
        if not content:
            # Not a warning: an empty note or an unset lot is ordinary, and
            # saying so on every label would bury the warnings that matter.
            continue

        if element.type == "text":
            canvas.text(
                content,
                box_mm=box,
                size_mm=element.size_mm,
                bold=element.bold,
                italic=element.italic,
                align=element.align,
                valign=element.valign,
                fit=element.fit,
            )
            continue

        if element.type == "qr":
            # Square, so the smaller side of the box decides.
            side_px = max(1, round(min(element.w_mm, element.h_mm) * canvas.dots_per_mm))
            canvas.image(render_qr(content, side_px), box_mm=box)
            continue

        if element.type == "swatch":
            colours = [part.strip().lstrip("#") for part in content.split(",") if part.strip()]
            if colours:
                canvas.swatch(colours, box_mm=box, shape=element.shape)
            continue

        if element.type == "barcode":
            width_px = max(1, round(element.w_mm * canvas.dots_per_mm))
            height_px = max(1, round(element.h_mm * canvas.dots_per_mm))
            try:
                code = render_barcode(
                    content,
                    symbology=element.symbology,
                    width_px=width_px,
                    height_px=height_px,
                )
            except BarcodeError as error:
                warnings.append(f"element {index + 1} (barcode, {element.symbology}): {error}")
                continue
            canvas.image(code, box_mm=box)

    return warnings


__all__ = ["Box", "LabelCanvas", "draw_template"]
