"""Offline stand of the part-render script (spec §15, plan task 6): renders the fixtures through the real Node
+ tracked bundle, checks pixels and golden hashes, measures samples with the sampled RSS of Python + Node.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
from PIL import Image

from backend.app.services.part_render_node import NodeRenderError, node_command, node_env, run_frames, verify_bundle
from backend.app.services.render_runtime import UnsupportedPlatform, locate

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
) -> dict:
    problems: list[str] = []
    objects: dict[int, dict] = {}
    if error is not None:
        problems.append(f"page error: {error.get('reason')}")
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


GOLDEN = FIXTURES / "golden.json"
_SELF = psutil.Process(os.getpid())


def default_node() -> Path | None:
    """The provisioned pin first, then a developer Node on PATH.

    Raises UnsupportedPlatform on a machine without an official Node build (consilium R5.3).
    """
    found = locate(Path(__file__).resolve().parents[2])
    if found is not None:
        return found.executable
    on_path = shutil.which("node")
    return Path(on_path) if on_path else None


def node_version(node: Path) -> str:
    return subprocess.run(
        [str(node), "--version"], capture_output=True, text=True, timeout=30, env=node_env()
    ).stdout.strip()


def _python_and_node_rss(node_pid: int) -> int:
    """Sampled every 50 ms by run_frames: this Python process (it streams the G-code) plus Node."""
    return _SELF.memory_info().rss + psutil.Process(node_pid).memory_info().rss


def render_fixture(node: Path, fixture: str, out_dir: Path, *, timeout: float = 120.0) -> dict:
    spec = FIXTURE_JOBS[fixture]
    job = dict(spec["job"], outputBytes=64 * 1024 * 1024)
    gcode = (FIXTURES / spec["gcode"]).read_bytes()
    version = node_version(node)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_frames(
            node_command(node),
            job,
            [gcode],
            env=node_env(),
            deadline_s=timeout,
            output_bytes=job["outputBytes"],
            rss_of=_python_and_node_rss,
        )
    except NodeRenderError as exc:
        return {
            "fixture": fixture,
            "node": version,
            "ok": False,
            "problems": [f"{exc.reason}: {exc.message}"],
            "sha256": {},
        }
    for object_id, png in result.pngs.items():
        (out_dir / f"{object_id}.png").write_bytes(png)
    report = evaluate_render(
        fixture, result.manifest, None, {i: out_dir / f"{i}.png" for i in result.pngs}, size=SIZE, min_pixels=MIN_PIXELS
    )
    report.update(
        node=version,
        sha256={str(i): hashlib.sha256(p).hexdigest() for i, p in sorted(result.pngs.items())},
        elapsed_ms=round(result.elapsed_s * 1000),
        peak_rss_python_and_node=result.peak_rss,
    )
    return report


def compare_golden(results: dict[str, dict], golden: dict) -> list[str]:
    problems: list[str] = []
    for fixture, expected in golden["png_sha256"].items():
        got = results.get(fixture)
        if got is None or not got.get("ok"):
            problems.append(f"{fixture}: not rendered")
            continue
        if got.get("node") != golden["node"]:
            return [f"golden skipped: node {got.get('node')} != {golden['node']}"]
        for object_id, sha in expected.items():
            if got["sha256"].get(object_id) != sha:
                problems.append(f"{fixture} {object_id}: sha256 differs from golden")
    return problems


def measure(node: Path, gcode: Path) -> dict:
    """All marked objects of a real sample; ids come from the slicer's object markers."""
    data = gcode.read_bytes()
    ids = sorted(
        {
            int(line.rsplit(b":", 1)[1])
            for line in data.splitlines()
            if line.startswith(b"; start printing object, unique label id:")
        }
    )
    job = {"size": SIZE, "outputBytes": 256 * 1024 * 1024, "objects": [{"id": i, "mode": "toolpath"} for i in ids]}
    started = time.monotonic()
    result = run_frames(
        node_command(node),
        job,
        [data],
        env=node_env(),
        deadline_s=3600,
        output_bytes=job["outputBytes"],
        rss_of=_python_and_node_rss,
    )
    return {
        "file": gcode.name,
        "mb": round(len(data) / 1e6, 1),
        "node": node_version(node),
        "objects": len(ids),
        "elapsed_s": round(time.monotonic() - started, 1),
        "peak_rss_python_and_node_mb": result.peak_rss >> 20,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="part_render_probe")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", type=Path)
    r.add_argument("--write-golden", action="store_true")
    m = sub.add_parser("measure")
    m.add_argument("files", nargs="+", type=Path)
    for p in (r, m):
        p.add_argument("--node", type=Path)
        p.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        node = args.node or default_node()
    except UnsupportedPlatform as exc:
        print(
            f"{exc}: part thumbnails use the top-view fallback here (no_runtime); --node tries another Node",
            file=sys.stderr,
        )
        return 3
    if node is None:
        print("no Node runtime: pass --node or provision one", file=sys.stderr)
        return 1
    verify_bundle()
    if args.cmd == "measure":
        report = {"measure": [measure(node, f) for f in args.files]}
        ok = True
    else:
        out = args.out or Path(tempfile.mkdtemp(prefix="part-render-probe-"))
        results = {f: render_fixture(node, f, out / f) for f in FIXTURE_JOBS}
        if args.write_golden and all(r["ok"] for r in results.values()):
            golden = {
                "renderer": 2,
                "node": node_version(node),
                "png_sha256": {f: r["sha256"] for f, r in results.items()},
            }
            GOLDEN.write_text(json.dumps(golden, indent=2) + "\n", encoding="utf-8")
        golden_problems = (
            compare_golden(results, json.loads(GOLDEN.read_text(encoding="utf-8")))
            if GOLDEN.exists()
            else ["golden.json missing: run render --write-golden"]
        )
        report = {"render": results, "golden": golden_problems or "match"}
        ok = all(r["ok"] for r in results.values()) and not golden_problems
    text = json.dumps(report, indent=2, default=str)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
