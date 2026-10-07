"""Part-render probe fixtures and pixel checks (spec §15).

What stays of the browser probe: the fixture jobs with their pixel expectations and
the check of a rendered set. Plan task 6 moves them into part_render_probe.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

FIXTURES = Path(__file__).resolve().parent / "data" / "render_probe"
SIZE = 512
MIN_PIXELS = 50

FIXTURE_JOBS: dict[str, dict] = {
    "two-objects": {
        "gcode": "two-objects.gcode",
        "job": {"size": SIZE, "objects": [{"id": 101, "mode": "toolpath"}, {"id": 202, "mode": "toolpath"}]},
        # 101 prints its top in T2 (#00AE42), the rest of both objects in T0 (#C0C0C0).
        "expect": {101: {"tools": [0, 2], "green": True}, 202: {"tools": [0], "green": False}},
    },
    "single-no-markers": {
        "gcode": "single-no-markers.gcode",
        "job": {"size": SIZE, "objects": [{"id": 1, "mode": "model", "bbox": [119, 119, 126, 126]}]},
        "expect": {1: {"tools": [0], "green": False, "method": "model"}},
    },
}


def pixel_counts(png: Path) -> dict:
    """Opaque, green (#00AE42-like) and silver (#C0C0C0-like) pixels -- the verify-colors rule of the research."""
    with Image.open(png) as img:
        rgba = img.convert("RGBA")
        size = [rgba.width, rgba.height]
        corner_alpha = rgba.getpixel((0, 0))[3]
        opaque = green = silver = 0
        raw = rgba.tobytes()  # getdata() is deprecated (Pillow 14); RGBA bytes are version-independent
        for i in range(0, len(raw), 4):
            r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
            if a == 0:
                continue
            opaque += 1
            if g > 50 and g > r * 1.5 and g > b * 1.5:
                green += 1
            if min(r, g, b) > 60 and max(r, g, b) - min(r, g, b) < 8:
                silver += 1
    return {"size": size, "opaque": opaque, "green": green, "silver": silver, "corner_alpha": corner_alpha}


def evaluate_render(
    fixture: str,
    manifest: dict | None,
    error: dict | None,
    pngs: dict[int, Path],
    *,
    size: int,
    min_pixels: int,
    browser_exit: int | None = None,
) -> dict:
    """``browser_exit``: the code when the browser ended by itself before the page finished, else None."""
    problems: list[str] = []
    objects: dict[int, dict] = {}
    if error is not None:
        problems.append(f"page error: {error.get('reason')}")
    elif manifest is None and browser_exit is not None:
        problems.append(f"the browser exited with {browser_exit} before the manifest")
    elif manifest is None:
        problems.append("no manifest before the deadline")
    else:
        by_id = {int(o["id"]): o for o in manifest.get("objects", [])}
        for object_id, expect in FIXTURE_JOBS[fixture]["expect"].items():
            entry = by_id.get(object_id)
            if entry is None or entry.get("method") == "missing":
                problems.append(f"{object_id}: not rendered ({entry and entry.get('reason')})")
                continue
            if entry.get("tools") != expect["tools"]:
                problems.append(f"{object_id}: tools {entry.get('tools')} != {expect['tools']}")
            if "method" in expect and entry.get("method") != expect["method"]:
                problems.append(f"{object_id}: method {entry.get('method')} != {expect['method']}")
            png = pngs.get(object_id)
            if png is None:
                problems.append(f"{object_id}: PNG missing")
                continue
            counts = pixel_counts(png)
            objects[object_id] = counts
            if counts["size"] != [size, size]:
                problems.append(f"{object_id}: size {counts['size']}")
            if counts["corner_alpha"] != 0:
                problems.append(f"{object_id}: background is not transparent")
            if counts["opaque"] < min_pixels:
                problems.append(f"{object_id}: almost empty ({counts['opaque']} opaque pixels)")
            if counts["silver"] < min_pixels:
                problems.append(f"{object_id}: expected silver pixels")
            if expect["green"] and counts["green"] < min_pixels:
                problems.append(f"{object_id}: expected green pixels")
            if not expect["green"] and counts["green"] >= min_pixels:
                problems.append(f"{object_id}: unexpected green pixels")
    return {"fixture": fixture, "ok": not problems, "problems": problems, "objects": objects}
