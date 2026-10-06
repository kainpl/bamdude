"""Render-browser probe: renders the bundled fixtures with a browser.

E1 runs it with a developer browser (``--browser``). E2 adds sandbox, network
and containment checks and runs it on every target (spec §15, §16). It ships
with the app, so the same command diagnoses a field install.

    python -m backend.app.render_browser_probe render --browser PATH
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
from PIL import Image

from backend.app.services.part_render_http import JobLimits, JobServer

APP_DIR = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "data" / "render_probe"
DEFAULT_BUNDLE = APP_DIR / "static" / "render-worker"
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
    fixture: str, manifest: dict | None, error: dict | None, pngs: dict[int, Path], *, size: int, min_pixels: int
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


def _tree_rss(root: psutil.Process) -> int:
    total = 0
    for proc in [root, *root.children(recursive=True)]:
        try:
            total += proc.memory_info().rss
        except psutil.Error:
            pass
    return total


def kill_tree(pid: int) -> list[int]:
    """Kill a process tree; return pids still alive afterwards (should be empty)."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    procs = [root, *root.children(recursive=True)]
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    return [p.pid for p in alive]


def run_render(browser_cmd: list[str], *, bundle_dir: Path, fixture: str, out_dir: Path, timeout: float) -> dict:
    spec = FIXTURE_JOBS[fixture]
    gcode = FIXTURES / spec["gcode"]
    out_dir.mkdir(parents=True, exist_ok=True)
    server = JobServer(
        bundle_dir=bundle_dir,
        job=spec["job"],
        open_gcode=lambda: gcode.open("rb"),
        gcode_bytes=gcode.stat().st_size,
        out_dir=out_dir,
        limits=JobLimits(png_bytes=8 * 1024 * 1024, total_bytes=64 * 1024 * 1024),
    )
    url = server.start()
    started = time.monotonic()
    peak = 0
    leftovers: list[int] = []
    with tempfile.TemporaryDirectory(prefix="bamdude-probe-") as tmp:
        profile = str(Path(tmp) / "profile")
        cmd = [part.replace("{profile}", profile).replace("{origin}", server.origin) for part in browser_cmd] + [url]
        # A file, not a pipe: Chromium logs to stderr, and a full pipe would stall the render.
        with (Path(tmp) / "stderr.log").open("w+b") as stderr:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)
            try:
                root = psutil.Process(proc.pid)
                deadline = started + timeout
                while time.monotonic() < deadline and not server.wait(0.1):
                    # The budget is the render child (Python + its job server) PLUS the whole browser tree.
                    peak = max(peak, psutil.Process().memory_info().rss + _tree_rss(root))
                    if proc.poll() is not None:
                        break
            finally:
                leftovers = kill_tree(proc.pid)
                proc.wait(timeout=5)
                server.close()
                stderr.seek(0)
                stderr_tail = stderr.read()[-4096:].decode(errors="replace")
    report = evaluate_render(
        fixture, server.outcome.manifest, server.outcome.error, server.outcome.pngs, size=SIZE, min_pixels=MIN_PIXELS
    )
    report.update(
        elapsed_ms=int((time.monotonic() - started) * 1000),
        peak_rss_python_and_browser=peak,
        leftover_pids=leftovers,
        browser_exit=proc.returncode,
        stderr_tail=stderr_tail,
    )
    if leftovers:
        report["ok"] = False
        report["problems"].append(f"processes left after kill: {leftovers}")
    return report


def _dev_browser_cmd(browser: str) -> list[str]:
    # E1 only: a developer browser with the minimum to render. E2 replaces this
    # with render_browser.launch_args (sandbox on, proxy rules, network off).
    return [
        browser,
        "--headless",
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
        "--user-data-dir={profile}",
        "--no-first-run",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render_browser_probe")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="render the bundled fixtures")
    render.add_argument("--browser", required=True, help="path to a Chromium / chrome-headless-shell executable")
    render.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    render.add_argument("--fixture", choices=[*FIXTURE_JOBS, "all"], default="all")
    render.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "bamdude-render-probe")
    render.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    fixtures = list(FIXTURE_JOBS) if args.fixture == "all" else [args.fixture]
    reports = [
        run_render(
            _dev_browser_cmd(args.browser),
            bundle_dir=args.bundle,
            fixture=f,
            out_dir=args.out / f,
            timeout=args.timeout,
        )
        for f in fixtures
    ]
    print(json.dumps(reports, indent=2))
    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
