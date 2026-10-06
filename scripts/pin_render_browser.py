"""Pin chrome-headless-shell for every platform BamDude ships (spec §6.1).

Reads the Chrome for Testing catalogue, downloads each platform's archive once
to hash it (CfT publishes no hashes) and rewrites backend/app/data/render_browser.json.
Run when the pin is reviewed: every BamDude release, and on a Critical/High
Chrome Stable security bulletin for V8, Blink, WebGL/ANGLE/SwiftShader or GPU (spec §6.3).

    python scripts/pin_render_browser.py [--channel Stable]
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

CATALOGUE = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
PLATFORMS = ("linux64", "linux-arm64", "mac-x64", "mac-arm64", "win64")
MANIFEST = Path(__file__).resolve().parents[1] / "backend" / "app" / "data" / "render_browser.json"
HEADERS = {"User-Agent": "BamDude render-browser pin"}


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60)


def _hash(url: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open(url) as resp:
        while block := resp.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="Stable")
    args = parser.parse_args(argv)

    with _open(CATALOGUE) as resp:
        catalogue = json.load(resp)
    channel = catalogue["channels"][args.channel]
    downloads = {d["platform"]: d["url"] for d in channel["downloads"]["chrome-headless-shell"]}
    missing = [p for p in PLATFORMS if p not in downloads]
    if missing:
        print(f"Chrome for Testing {channel['version']} has no chrome-headless-shell for {missing}", file=sys.stderr)
        return 1

    platforms = {}
    for key in PLATFORMS:
        sha256, size = _hash(downloads[key])
        platforms[key] = {"url": downloads[key], "sha256": sha256, "bytes": size}
        print(f"{key}: {size} bytes {sha256}")

    manifest = {
        "product": "chrome-headless-shell",
        "source": "chrome-for-testing",
        "channel": args.channel,
        "version": channel["version"],
        "pinned_at": date.today().isoformat(),
        "platforms": platforms,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"pinned {channel['version']} -> {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
