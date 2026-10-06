from pathlib import Path

from PIL import Image

from backend.app.render_browser_probe import FIXTURE_JOBS, evaluate_render, pixel_counts


def _png(path: Path, pixels: dict[tuple[int, int], tuple[int, int, int, int]], size: int = 8) -> Path:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for xy, rgba in pixels.items():
        img.putpixel(xy, rgba)
    img.save(path)
    return path


def test_pixel_counts_reads_green_silver_and_transparency(tmp_path: Path):
    png = _png(
        tmp_path / "a.png", {(1, 1): (0, 174, 66, 255), (2, 2): (192, 192, 192, 255), (3, 3): (192, 192, 192, 255)}
    )
    counts = pixel_counts(png)
    assert counts == {"size": [8, 8], "opaque": 3, "green": 1, "silver": 2, "corner_alpha": 0}


def test_fixture_jobs_name_every_object_with_expectations():
    assert set(FIXTURE_JOBS) == {"two-objects", "single-no-markers"}
    two = FIXTURE_JOBS["two-objects"]
    assert [o["id"] for o in two["job"]["objects"]] == [101, 202]
    assert two["expect"][101] == {"tools": [0, 2], "green": True}
    single = FIXTURE_JOBS["single-no-markers"]
    assert single["job"]["objects"] == [{"id": 1, "mode": "model", "bbox": [119, 119, 126, 126]}]


def test_evaluate_render_requires_manifest_pngs_and_colours(tmp_path: Path):
    green = _png(
        tmp_path / "101.png",
        {(x, 2): (0, 174, 66, 255) for x in range(8)} | {(x, 5): (192, 192, 192, 255) for x in range(8)},
    )
    silver = _png(tmp_path / "202.png", {(x, 4): (192, 192, 192, 255) for x in range(8)})
    manifest = {
        "renderer": 1,
        "objects": [
            {"id": 101, "method": "toolpath", "tools": [0, 2], "width": 8, "height": 8},
            {"id": 202, "method": "toolpath", "tools": [0], "width": 8, "height": 8},
        ],
    }
    report = evaluate_render("two-objects", manifest, None, {101: green, 202: silver}, size=8, min_pixels=4)
    assert report["ok"] is True

    missing_colour = evaluate_render("two-objects", manifest, None, {101: silver, 202: silver}, size=8, min_pixels=4)
    assert missing_colour["ok"] is False
    assert "101: expected green pixels" in missing_colour["problems"]


def test_evaluate_render_fails_on_page_error():
    report = evaluate_render(
        "two-objects", None, {"reason": "parse_failed", "message": "x"}, {}, size=512, min_pixels=50
    )
    assert report["ok"] is False
    assert report["problems"] == ["page error: parse_failed"]
