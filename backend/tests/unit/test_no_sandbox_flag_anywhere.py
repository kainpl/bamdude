"""Spec §6.3: the Chromium sandbox is never disabled automatically.

A platform exception exists only through a revision of the spec; this test
then gets an allowlist entry naming that revision. Until then: none.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCANNED = ["backend/app", "scripts", "deploy", "install", "installers", "Dockerfile", ".github"]
FORBIDDEN = ("--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu-sandbox")
SKIP_SUFFIXES = {".png", ".zip", ".exe", ".dll", ".pak", ".bin"}
ALLOWLIST: dict[str, str] = {}


def _files():
    for entry in SCANNED:
        path = REPO / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from (p for p in path.rglob("*") if p.is_file() and p.suffix not in SKIP_SUFFIXES)


def test_no_source_disables_the_chromium_sandbox():
    hits = []
    for path in _files():
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWLIST or "/__pycache__/" in f"/{rel}":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{rel}: {flag}" for flag in FORBIDDEN if flag in text]
    assert hits == []
