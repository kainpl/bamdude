"""Drawing object markers onto the plate picture the bot sends.

The web overlay places its markers as DOM nodes over the same image. The bot
has no DOM, so the numbers are burnt into the pixels here — from the same
percentages the browser reads, computed once in ``plate_markers.py``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from backend.app.services.plate_marker_render import (
    MARKER_RADIUS_PX,
    PlateMarker,
    render_markers,
    top_view_png,
)

pytestmark = pytest.mark.unit

WHITE = (255, 255, 255)


def _blank(size: int = 512) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), WHITE).save(buf, format="PNG")
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_the_result_is_a_png_of_the_same_size():
    out = render_markers(_blank(), [PlateMarker(id=941, x=50.0, y=50.0, skipped=False)])

    assert Image.open(io.BytesIO(out)).format == "PNG"
    assert _open(out).size == (512, 512)


def test_no_markers_still_returns_a_decodable_image():
    """The plate may genuinely have nothing skippable on it."""
    out = render_markers(_blank(), [])

    assert _open(out).size == (512, 512)


def test_a_marker_is_actually_drawn():
    out = render_markers(_blank(), [PlateMarker(id=7, x=50.0, y=50.0, skipped=False)])

    assert _open(out).getpixel((256, 256)) != WHITE


def test_the_top_left_extreme_is_pulled_fully_into_frame():
    """0%,0% is the centre of a circle, not its corner — half of it would hang
    off the canvas and the number inside would be unreadable."""
    out = render_markers(_blank(), [PlateMarker(id=1, x=0.0, y=0.0, skipped=False)])

    img = _open(out)
    assert img.getpixel((MARKER_RADIUS_PX, MARKER_RADIUS_PX)) != WHITE
    assert img.getpixel((511, 511)) == WHITE, "ink where no marker was asked for"


def test_the_bottom_right_extreme_is_pulled_fully_into_frame():
    out = render_markers(_blank(), [PlateMarker(id=1, x=100.0, y=100.0, skipped=False)])

    img = _open(out)
    assert img.getpixel((511 - MARKER_RADIUS_PX, 511 - MARKER_RADIUS_PX)) != WHITE
    assert img.getpixel((0, 0)) == WHITE


def test_a_skipped_object_looks_different_from_a_live_one():
    """The operator has to tell at a glance what is already cancelled — the
    picture is the whole interface here, there is no list beside it."""
    live = _open(render_markers(_blank(), [PlateMarker(id=5, x=50.0, y=50.0, skipped=False)]))
    skipped = _open(render_markers(_blank(), [PlateMarker(id=5, x=50.0, y=50.0, skipped=True)]))

    assert live.tobytes() != skipped.tobytes()


def test_every_marker_lands():
    out = _open(
        render_markers(
            _blank(),
            [
                PlateMarker(id=1, x=20.0, y=20.0, skipped=False),
                PlateMarker(id=2, x=80.0, y=80.0, skipped=False),
            ],
        )
    )

    assert out.getpixel((102, 102)) != WHITE
    assert out.getpixel((409, 409)) != WHITE


def _ink_extent(img: Image.Image, row: int) -> tuple[int, int]:
    xs = [x for x in range(img.width) if img.getpixel((x, row)) != WHITE]
    return (min(xs), max(xs)) if xs else (-1, -1)


def test_a_long_id_widens_the_pin_rather_than_clipping_it():
    """``identify_id`` is whatever the slicer assigned — four digits happen."""
    short = _open(render_markers(_blank(), [PlateMarker(id=1, x=50.0, y=50.0, skipped=False)]))
    long_ = _open(render_markers(_blank(), [PlateMarker(id=12345, x=50.0, y=50.0, skipped=False)]))

    short_l, short_r = _ink_extent(short, 256)
    long_l, long_r = _ink_extent(long_, 256)
    assert long_r - long_l > short_r - short_l


def test_a_long_id_at_the_edge_is_still_whole():
    img = _open(render_markers(_blank(), [PlateMarker(id=12345, x=0.0, y=50.0, skipped=False)]))

    left, right = _ink_extent(img, 256)
    assert left >= 0
    assert right < 512, "the pin ran off the canvas"
    assert right - left > MARKER_RADIUS_PX * 2, "widened, not squeezed back into a circle"


# ── the top-view guard ──────────────────────────────────────────────────────


def _three_mf(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "job.gcode.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_the_top_view_is_read_for_the_asked_plate(tmp_path: Path):
    expected = _blank(64)
    path = _three_mf(tmp_path, {"Metadata/top_1.png": _blank(32), "Metadata/top_2.png": expected})

    assert top_view_png(path, 2) == expected


def test_there_is_no_fallback_to_the_three_quarter_render(tmp_path: Path):
    """⚠️ The load-bearing test of this module.

    ``GET /printers/{id}/cover?view=top`` degrades through ``plate_N.png`` and
    ``thumbnail.png`` so a camera card always shows something. Those are ¾
    renders: markers computed in top-down space land on the wrong parts, and
    convincingly so. A missing picture is recoverable; a lying one gets the
    wrong object cancelled, irreversibly."""
    path = _three_mf(
        tmp_path,
        {"Metadata/plate_1.png": _blank(64), "Metadata/thumbnail.png": _blank(64), "Metadata/top_9.png": _blank(64)},
    )

    assert top_view_png(path, 1) is None


def test_a_missing_file_is_no_picture_rather_than_an_error(tmp_path: Path):
    assert top_view_png(tmp_path / "gone.3mf", 1) is None


def test_an_unreadable_archive_is_no_picture_rather_than_an_error(tmp_path: Path):
    broken = tmp_path / "broken.3mf"
    broken.write_bytes(b"not a zip at all")

    assert top_view_png(broken, 1) is None
