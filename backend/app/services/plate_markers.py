"""Where an object's marker sits on the plate image.

**The one implementation.** It was TypeScript
(``frontend/src/components/plateDialogLayout.ts::markerPosition``) until
2026-08-16, when the Telegram bot needed the same numbers to draw the picture
it sends. The choice then was between porting it and keeping two copies.

⚠️ Two copies were rejected, and not on taste. A marker that drifts from the
plate points **confidently at the wrong part**, and the action the operator
takes next — skipping an object — cannot be undone. This codebase has already
paid for duplicated judgements twice over: two answers to "does this 3MF hold
G-code", three readings of ``.gcode.3mf``. So the TS function was deleted, not
deprecated, and the frontend consumes what this returns.

Four sources in descending order of trust; the first with usable data wins.
The order is part of the contract — see each branch.

⚠️ **One deliberate divergence from the TypeScript it replaces.** Branch 2
guards against a degenerate ``bbox_all`` (zero width or height) and falls
through to branch 3. The original divided regardless, which yields ``NaN`` or
``Infinity`` — in a browser that quietly places the marker nowhere; here it
would reach a drawing library. A bbox with no extent is missing data, not a
layout, so it is treated as missing.
"""

from __future__ import annotations

import math

# The ``Metadata/top_N.png`` render leaves roughly this much margin on each
# side, as a percentage of the image. Content therefore occupies the middle
# 100 - 2*PADDING.
_RENDER_PADDING_PCT = 8.0

# Assumed plate edge in millimetres when nothing tells us the real extent.
# 256 is the Bambu bed the great majority of these files are sliced for.
_ASSUMED_PLATE_MM = 256.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def marker_position(
    obj: dict,
    idx: int,
    total: int,
    bbox_all: list | tuple | None,
) -> dict[str, float]:
    """Marker centre as percentages of the image box: ``{"x": .., "y": ..}``.

    Args:
        obj: One entry of the objects payload — ``x``/``y`` may be ``None``,
            ``norm`` marks the coordinates as already normalised 0..1.
        idx / total: Position in the list, used only by the grid fallback.
        bbox_all: ``[xMin, yMin, xMax, yMax]`` in millimetres, or ``None``.
    """
    x, y = obj.get("x"), obj.get("y")

    # 1. Normalised pick-PNG centroid — matches what the printer's own screen
    #    shows, so this is the most trustworthy source there is.
    if obj.get("norm") and x is not None and y is not None:
        return {"x": _clamp(x * 100.0, 2.0, 98.0), "y": _clamp(y * 100.0, 2.0, 98.0)}

    # 2. Millimetres mapped through the bbox the top view was rendered from.
    if x is not None and y is not None and bbox_all:
        x_min, y_min, x_max, y_max = (float(v) for v in bbox_all[:4])
        span_x = x_max - x_min
        span_y = y_max - y_min
        if span_x > 0 and span_y > 0:
            content = 100.0 - _RENDER_PADDING_PCT * 2
            return {
                "x": _clamp(_RENDER_PADDING_PCT + ((x - x_min) / span_x) * content, 5.0, 95.0),
                # ⚠️ Image Y grows DOWNWARD, 3D Y grows toward the BACK of the
                # plate. Drop this inversion and front and back swap — which
                # looks entirely plausible and points at the wrong part.
                "y": _clamp(_RENDER_PADDING_PCT + ((y_max - y) / span_y) * content, 5.0, 95.0),
            }

    # 3. No bbox — assume a full plate.
    if x is not None and y is not None:
        return {
            "x": _clamp((x / _ASSUMED_PLATE_MM) * 100.0, 5.0, 95.0),
            "y": _clamp(100.0 - (y / _ASSUMED_PLATE_MM) * 100.0, 5.0, 95.0),
        }

    # 4. No coordinates at all — lay them out on a grid so every object is
    #    still reachable. ⚠️ Positions here are MEANINGLESS; the caller must
    #    say so (``positions_approximate``), because a picture that looks like
    #    a map and is a legend is worse than no picture.
    cols = max(1, math.ceil(math.sqrt(total or 1)))
    rows = max(1, math.ceil((total or 1) / cols))
    return {
        "x": 15.0 + (idx % cols) * (70.0 / cols) + 35.0 / cols,
        "y": 15.0 + (idx // cols) * (70.0 / rows) + 35.0 / rows,
    }
