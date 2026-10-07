"""Spec §15: the browser path is gone for good -- product sources, image, installers and CI name none of it."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCANNED = [
    ROOT / "backend" / "app",
    ROOT / "frontend" / "src",
    ROOT / "frontend" / "scripts",
    ROOT / "Dockerfile",
    ROOT / "install",
    ROOT / "installers" / "windows",
    ROOT / ".github" / "workflows",
]
NAMES = (
    "chrome-headless-shell",
    "render_browser",
    "render-worker",
    "render_sandbox",
    "part_render_http",
    "--no-sandbox",
)


def _tracked(bases: list[Path]) -> list[Path]:
    """Tracked files only: an installer build's gitignored staging copy is not the product."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *(base.relative_to(ROOT).as_posix() for base in bases)],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [ROOT / name for name in out.split("\0") if name]


def test_no_browser_names_left():
    hits = []
    for path in _tracked(SCANNED):
        if path.suffix not in (".py", ".ts", ".tsx", ".mjs", ".sh", ".yml", ".iss", ".json", "") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{path.relative_to(ROOT)}: {name}" for name in NAMES if name in text]
    assert hits == []
