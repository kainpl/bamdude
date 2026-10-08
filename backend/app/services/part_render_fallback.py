"""Fallback methods of a plate's instances and the derived small size (spec §5.3).

Pure functions over bytes the child already read: numpy + Pillow, no I/O. ``top_mask`` cuts one instance
out of the slicer's own ``Metadata/top_N.png`` by its ``Metadata/pick_N.png`` colour. The id is decoded
exactly, unlike threemf_parser_core's discovery, whose grey and dark-colour heuristics would reject
ID 265 = RGB(9, 1, 0).
"""

from __future__ import annotations

import io
import json

import numpy as np
from PIL import Image

from backend.app.services.part_render_protocol import SMALL_SIZE

MARGIN = 0.08  # of the crop's longer side, on every edge


def decode_ids(pick: Image.Image) -> np.ndarray:
    """``R | G<<8 | B<<16`` of every pixel."""
    rgb = np.asarray(pick.convert("RGB"), dtype=np.uint32)
    return rgb[..., 0] | (rgb[..., 1] << 8) | (rgb[..., 2] << 16)


def valid_pair(top_png: bytes, pick_png: bytes) -> tuple[Image.Image, np.ndarray] | None:
    """The decoded pair, or None when it is refused: the same bytes (preview_transform.inject_source writes
    one picture under all three names), sizes that differ, or an image that does not decode."""
    if top_png == pick_png:
        return None
    try:
        with Image.open(io.BytesIO(top_png)) as top_img, Image.open(io.BytesIO(pick_png)) as pick_img:
            if top_img.size != pick_img.size:
                return None
            return top_img.convert("RGBA"), decode_ids(pick_img)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=False)
    return out.getvalue()


def _resize(img: Image.Image, size: int) -> Image.Image:
    # premultiplied, so the transparent surround does not darken the instance's edge
    return img.convert("RGBa").resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")


def top_mask(pair: tuple[Image.Image, np.ndarray], identify_id: int) -> bytes | None:
    """The instance on a transparent square at the top image's own resolution, cropped to its mask with a
    margin; None when it has no pixel. Never upscaled: 256 such masters stay a fraction of one top_N.png,
    which keeps a fallback-only plate inside ATTEMPT_BYTES (plan E3, R1)."""
    top, ids = pair
    mask = ids == identify_id
    if not mask.any():
        return None
    rgba = np.array(top, dtype=np.uint8)
    rgba[..., 3] = np.where(mask, rgba[..., 3], 0)
    rows, cols = np.nonzero(mask)
    y0, y1, x0, x1 = int(rows.min()), int(rows.max()) + 1, int(cols.min()), int(cols.max()) + 1
    side = max(y1 - y0, x1 - x0)
    pad = max(2, round(side * MARGIN))
    canvas = Image.new("RGBA", (side + 2 * pad, side + 2 * pad), (0, 0, 0, 0))
    canvas.paste(
        Image.fromarray(rgba[y0:y1, x0:x1], "RGBA"),
        (pad + (side - (x1 - x0)) // 2, pad + (side - (y1 - y0)) // 2),
    )
    return _png(canvas)


def small(master_png: bytes, *, size: int = SMALL_SIZE) -> bytes:
    with Image.open(io.BytesIO(master_png)) as img:
        return _png(_resize(img, size))


def model_bbox(plate_json: bytes | None, identify_id: int) -> list[float] | None:
    """``bbox_objects[].bbox`` of the object in ``Metadata/plate_N.json`` -- spec §5.3, condition 3."""
    if not plate_json:
        return None
    try:
        data = json.loads(plate_json)
    except ValueError:
        return None
    entries = data.get("bbox_objects") if isinstance(data, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        try:
            if int(entry.get("id")) != identify_id:
                continue
        except (TypeError, ValueError, AttributeError):
            continue
        bbox = entry.get("bbox")
        if isinstance(bbox, list) and len(bbox) >= 4 and all(type(v) in (int, float) for v in bbox[:4]):
            return [float(v) for v in bbox[:4]]
        return None
    return None
