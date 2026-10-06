import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

MANIFEST = Path(__file__).resolve().parents[2] / "app" / "data" / "render_browser.json"
PLATFORMS = {"linux64", "linux-arm64", "mac-x64", "mac-arm64", "win64"}


def test_manifest_pins_one_version_for_every_platform():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["product"] == "chrome-headless-shell"
    assert data["source"] == "chrome-for-testing"
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", data["version"])
    date.fromisoformat(data["pinned_at"])
    assert set(data["platforms"]) == PLATFORMS
    for key, entry in data["platforms"].items():
        url = urlparse(entry["url"])
        assert url.scheme == "https"
        assert url.hostname == "storage.googleapis.com"
        assert data["version"] in entry["url"], key
        assert f"chrome-headless-shell-{key}.zip" in entry["url"], key
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), key
        assert isinstance(entry["bytes"], int) and entry["bytes"] > 10_000_000, key
