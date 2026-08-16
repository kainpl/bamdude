"""Burn object-ID markers into the plate picture, for clients with no DOM.

The web overlay lays its markers over the image as DOM nodes, positioned by
the percentages ``plate_markers.marker_position`` computes. Telegram gets a
photo and nothing else, so the same percentages are drawn into the pixels
here. Both sides read one placement function — see that module for why.

⚠️ **Only ever draw on the top-down render.** Bambu bundles two pictures per
plate: ``Metadata/top_N.png`` looks straight down, ``Metadata/plate_N.png`` is
a ¾ view. The marker percentages are top-down coordinates. Laid over the ¾
render they land on the wrong parts — not obviously wrong, *plausibly* wrong,
which is worse: the operator presses the number under the part they meant and
cancels a different one. There is no undo for a skipped object.

That is why ``top_view_png`` has no fallback chain, unlike
``GET /printers/{id}/cover?view=top``, which degrades through ``plate_N.png``
to ``thumbnail.png`` so a printer card always shows *something*. Here a
missing picture is the correct answer; the caller sends the plain cover and
says the positions are unavailable.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Marker geometry, in absolute pixels rather than as a share of the plate.
#
# Same reasoning the React overlay records for its fixed ``w-6 h-6``: on a
# bigger plate the readability win is the *gap* between markers, not fatter
# markers. Tuned against the 512px render Bambu ships — the only size we have
# ever seen in a 3MF — and left alone on anything smaller, where the numbers
# would crowd but at least stay legible.
MARKER_RADIUS_PX = 17
_FONT_SIZE_PX = 19
_OUTLINE_PX = 2
_LABEL_PADDING_PX = 6  # breathing room each side of a label too wide for the circle

_LIVE_FILL = (0, 174, 66)  # bambu-green, as the overlay uses
_LIVE_TEXT = (0, 0, 0)
_SKIPPED_FILL = (239, 68, 68)  # red-500
_SKIPPED_TEXT = (255, 255, 255)
_OUTLINE = (255, 255, 255)


class PlateMarker(NamedTuple):
    """One numbered pin: the object's ID, where it goes, and its state.

    ⚠️ ``x``/``y`` are percentages of the image box, straight out of
    ``plate_markers.marker_position`` — never millimetres and never pixels.
    """

    id: int
    x: float
    y: float
    skipped: bool


def top_view_png(three_mf: Path, plate_index: int) -> bytes | None:
    """The top-down render for one plate, or ``None``.

    Deliberately no fallback and deliberately no raise: a caller that cannot
    get this picture must say so rather than draw on another one (module
    docstring). An unreadable or absent 3MF is the same answer as a 3MF
    without the member.
    """
    member = f"Metadata/top_{plate_index}.png"
    try:
        with zipfile.ZipFile(three_mf, "r") as zf:
            if member not in zf.namelist():
                logger.debug("No %s in %s — no top view to mark up", member, three_mf.name)
                return None
            return zf.read(member)
    except (OSError, zipfile.BadZipFile) as exc:
        logger.debug("Cannot read %s for a top view: %s", three_mf, exc)
        return None


def render_markers(image: bytes, markers: list[PlateMarker]) -> bytes:
    """Draw the markers onto ``image`` and return a new PNG of the same size.

    An empty list is a normal outcome — a plate can hold nothing skippable —
    and returns the picture re-encoded rather than an error.
    """
    with Image.open(io.BytesIO(image)) as src:
        canvas = src.convert("RGB")

    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=_FONT_SIZE_PX)
    width, height = canvas.size

    for marker in markers:
        label = str(marker.id)
        # Bambu's identify_id is whatever the slicer assigned — three and four
        # digits are ordinary. A fixed circle clips those, so the pin grows
        # sideways into a pill and keeps its height. Height is what reads as
        # "same kind of thing" across the plate; width is free.
        half_w = max(MARKER_RADIUS_PX, _text_width(draw, label, font) / 2 + _LABEL_PADDING_PX)

        # A percentage is the marker's CENTRE, so 0% would hang half the pin off
        # the canvas with the number inside it clipped. Pull the centre in
        # instead of letting PIL crop it.
        cx = _clamp(marker.x / 100.0 * width, half_w, width - 1 - half_w)
        cy = _clamp(marker.y / 100.0 * height, MARKER_RADIUS_PX, height - 1 - MARKER_RADIUS_PX)
        fill = _SKIPPED_FILL if marker.skipped else _LIVE_FILL
        text_fill = _SKIPPED_TEXT if marker.skipped else _LIVE_TEXT

        draw.rounded_rectangle(
            [cx - half_w, cy - MARKER_RADIUS_PX, cx + half_w, cy + MARKER_RADIUS_PX],
            radius=MARKER_RADIUS_PX,
            fill=fill,
            outline=_OUTLINE,
            width=_OUTLINE_PX,
        )
        draw.text((cx, cy), label, font=font, fill=text_fill, anchor="mm")
        if marker.skipped:
            # The overlay uses a CSS line-through; here it is a literal line.
            # Colour alone would carry it on a screen, but this photo is looked
            # at on a phone, often outdoors.
            draw.line([cx - half_w * 0.7, cy, cx + half_w * 0.7, cy], fill=text_fill, width=2)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
