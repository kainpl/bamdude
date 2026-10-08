"""Fallback methods of a plate's instances (spec §5.3): top_mask over the slicer's own top/pick pair."""

import io
import json

import numpy as np
from PIL import Image

from backend.app.services import part_render_fallback as fb


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _pick(ids: dict[tuple[int, int, int, int], int], size=(64, 64)) -> bytes:
    """A pick image: each box (x0, y0, x1, y1) painted with its id as R | G<<8 | B<<16."""
    img = Image.new("RGB", size, (0, 0, 0))
    for (x0, y0, x1, y1), oid in ids.items():
        img.paste((oid & 0xFF, (oid >> 8) & 0xFF, (oid >> 16) & 0xFF), (x0, y0, x1, y1))
    return _png(img)


def _top(size=(64, 64)) -> bytes:
    return _png(Image.new("RGBA", size, (200, 30, 30, 255)))


def test_ids_decode_exactly_without_dark_colour_heuristics():
    pick = Image.open(io.BytesIO(_pick({(0, 0, 4, 4): 265})))
    assert fb.decode_ids(pick)[0, 0] == 265  # RGB(9, 1, 0) is a valid id, not "dark"


def test_a_pair_of_identical_bytes_is_refused():
    same = _top()
    assert fb.valid_pair(same, same) is None


def test_a_pair_of_different_sizes_is_refused():
    assert fb.valid_pair(_top((64, 64)), _pick({(0, 0, 4, 4): 1}, size=(32, 32))) is None


def test_a_pair_that_does_not_decode_is_refused():
    assert fb.valid_pair(b"not a png", _pick({(0, 0, 4, 4): 1})) is None


def test_top_mask_keeps_the_instance_and_clears_the_rest():
    pair = fb.valid_pair(_top(), _pick({(10, 10, 30, 20): 101, (40, 40, 60, 60): 202}))
    png = fb.top_mask(pair, 101)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    # native resolution: the 20x10 instance, centred on a 20 + 2 * 2 square (spec §5.3: no upscale)
    assert img.size == (24, 24)
    alpha = np.asarray(img)[..., 3]
    assert alpha[0, 0] == 0 and alpha[-1, -1] == 0  # margin and the other instance are transparent
    assert alpha[12, 12] == 255  # the centre is the instance itself
    assert tuple(np.asarray(img)[12, 12][:3]) == (200, 30, 30)


def test_top_mask_of_an_absent_id_is_none():
    pair = fb.valid_pair(_top(), _pick({(10, 10, 30, 20): 101}))
    assert fb.top_mask(pair, 999) is None


def test_small_is_128():
    pair = fb.valid_pair(_top(), _pick({(10, 10, 30, 20): 101}))
    small = Image.open(io.BytesIO(fb.small(fb.top_mask(pair, 101))))
    assert small.size == (128, 128)


def test_model_bbox_reads_bbox_objects_by_id():
    plate = json.dumps({"bbox_objects": [{"id": 7, "name": "a", "bbox": [1, 2, 3, 4]}]}).encode()
    assert fb.model_bbox(plate, 7) == [1.0, 2.0, 3.0, 4.0]
    assert fb.model_bbox(plate, 8) is None
    assert fb.model_bbox(None, 7) is None
    assert fb.model_bbox(b"{broken", 7) is None
    assert fb.model_bbox(json.dumps({"bbox_objects": [{"id": 7, "bbox": [1, "x"]}]}).encode(), 7) is None
