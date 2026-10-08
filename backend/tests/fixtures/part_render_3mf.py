"""Small sliced 3MFs for the part-render tests (plan E3, task 16): real G-code of the E1b fixtures."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from PIL import Image

PROBE = Path(__file__).resolve().parents[2] / "app" / "data" / "render_probe"


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def pick_png(boxes: dict[int, tuple[int, int, int, int]], size: tuple[int, int] = (64, 64)) -> bytes:
    """A pick image: each box (x0, y0, x1, y1) painted with its id as R | G<<8 | B<<16."""
    img = Image.new("RGB", size, (0, 0, 0))
    for oid, box in boxes.items():
        img.paste((oid & 0xFF, (oid >> 8) & 0xFF, (oid >> 16) & 0xFF), box)
    return png(img)


def top_png(size: tuple[int, int] = (64, 64), colour=(200, 30, 30, 255)) -> bytes:
    return png(Image.new("RGBA", size, colour))


def gcode(name: str) -> bytes:
    return (PROBE / f"{name}.gcode").read_bytes()


def write_3mf(
    path: Path,
    plates: dict[int, bytes],
    *,
    objects: dict[int, dict[int, str]] | None = None,
    top: dict[int, bytes] | None = None,
    pick: dict[int, bytes] | None = None,
    plate_json: dict[int, dict] | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        for number, body in plates.items():
            zf.writestr(f"Metadata/plate_{number}.gcode", body)
        if objects:
            xml = "".join(
                f'<plate><metadata key="index" value="{number}"/>'
                + "".join(f'<object identify_id="{oid}" name="{name}" skipped="false"/>' for oid, name in objs.items())
                + "</plate>"
                for number, objs in objects.items()
            )
            zf.writestr("Metadata/slice_info.config", f"<config>{xml}</config>")
        for number, body in (top or {}).items():
            zf.writestr(f"Metadata/top_{number}.png", body)
        for number, body in (pick or {}).items():
            zf.writestr(f"Metadata/pick_{number}.png", body)
        for number, body in (plate_json or {}).items():
            zf.writestr(f"Metadata/plate_{number}.json", json.dumps(body))
    return path


def two_objects_3mf(path: Path, *, with_pair: bool = True) -> Path:
    """Fixture `two-objects` as plate 1: ids 101 and 202 with markers, and a valid top/pick pair."""
    return write_3mf(
        path,
        {1: gcode("two-objects")},
        objects={1: {101: "Bracket", 202: "Clip"}},
        top={1: top_png()} if with_pair else None,
        pick={1: pick_png({101: (4, 4, 20, 20), 202: (30, 30, 50, 50)})} if with_pair else None,
    )


def single_object_3mf(path: Path) -> Path:
    """Fixture `single-no-markers` as plate 1: one object, no markers, its bbox in plate_1.json."""
    return write_3mf(
        path,
        {1: gcode("single-no-markers")},
        objects={1: {1: "Cube"}},
        plate_json={1: {"bbox_objects": [{"id": 1, "name": "Cube", "bbox": [119, 119, 126, 126]}]}},
    )
