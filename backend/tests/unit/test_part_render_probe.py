from pathlib import Path

import pytest
from PIL import Image

from backend.app import part_render_probe
from backend.app.part_render_probe import FIXTURE_JOBS, evaluate_render, pixel_counts
from backend.app.services import render_runtime


def _pinned_node() -> Path | None:
    """The provisioned pin first, a developer Node on PATH only as a fallback (spec §6.1)."""
    try:
        return part_render_probe.default_node()
    except render_runtime.UnsupportedPlatform:
        return None


NODE = _pinned_node()
needs_node = pytest.mark.skipif(NODE is None, reason="no pinned Node and none on PATH")


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


def test_compare_golden_reports_each_difference():
    golden = {"renderer": 2, "node": "v1", "png_sha256": {"two-objects": {"101": "a" * 64, "202": "b" * 64}}}
    results = {"two-objects": {"ok": True, "node": "v1", "sha256": {"101": "a" * 64, "202": "c" * 64}}}
    assert part_render_probe.compare_golden(results, golden) == ["two-objects 202: sha256 differs from golden"]


def test_compare_golden_refuses_another_node_version():
    golden = {"renderer": 2, "node": "v1", "png_sha256": {"two-objects": {"101": "a" * 64}}}
    results = {"two-objects": {"ok": True, "node": "v2", "sha256": {"101": "a" * 64}}}
    assert part_render_probe.compare_golden(results, golden) == ["golden not comparable: run on node v2, written on v1"]


def test_compare_golden_reports_a_failed_render_instead_of_raising():
    golden = {"renderer": 2, "node": "v1", "png_sha256": {"two-objects": {"101": "a" * 64}}}
    results = {"two-objects": {"ok": False, "node": "v1", "problems": ["crashed: x"], "sha256": {}}}
    assert part_render_probe.compare_golden(results, golden) == ["two-objects: not rendered"]


@needs_node
def test_render_fixture_meets_the_pixel_expectations(tmp_path):
    report = part_render_probe.render_fixture(NODE, "two-objects", tmp_path)
    assert report["ok"], report["problems"]
    assert set(report["sha256"]) == {"101", "202"}


@needs_node
def test_a_runtime_failure_is_a_report_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setitem(part_render_probe.FIXTURE_JOBS["two-objects"], "gcode", "single-no-markers.gcode")
    monkeypatch.setattr(part_render_probe, "node_command", lambda node: [str(node), "-e", "process.exit(9)"])
    report = part_render_probe.render_fixture(NODE, "two-objects", tmp_path)
    assert report["ok"] is False and report["node"].startswith("v") and report["problems"][0].startswith("crashed")


def test_the_probe_on_a_platform_without_official_node_says_so(monkeypatch, capsys):
    def unsupported(app_dir):
        raise render_runtime.UnsupportedPlatform("no official Node.js build for linux/armv7l")

    monkeypatch.setattr(part_render_probe, "locate", unsupported)
    assert part_render_probe.main(["render"]) == 3
    assert "top-view fallback" in capsys.readouterr().err


def test_the_probe_falls_back_to_node_on_path_when_nothing_is_provisioned(monkeypatch):
    monkeypatch.setattr(part_render_probe, "locate", lambda app_dir: None)
    monkeypatch.setattr(part_render_probe.shutil, "which", lambda name: "/usr/bin/node")
    assert part_render_probe.default_node() == Path("/usr/bin/node")
