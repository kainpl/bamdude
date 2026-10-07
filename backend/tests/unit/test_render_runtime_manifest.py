"""The Node pin (spec §6.1): one official version, the five platforms of PLATFORMS, each archive hashed."""

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from backend.app.services.render_runtime import MANIFEST, PLATFORMS


def test_manifest_pins_one_official_node_24_for_every_platform():
    data = json.loads(Path(MANIFEST).read_text(encoding="utf-8"))
    assert data["product"] == "node"
    assert data["source"] == "nodejs.org/dist"
    assert re.fullmatch(r"v24\.\d+\.\d+", data["version"])
    date.fromisoformat(data["pinned_at"])
    assert set(data["platforms"]) == set(PLATFORMS)
    for key, entry in data["platforms"].items():
        suffix = "zip" if key == "win-x64" else "tar.xz"
        url = urlparse(entry["url"])
        assert url.scheme == "https", key
        assert url.hostname == "nodejs.org", key
        assert url.path == f"/dist/{data['version']}/node-{data['version']}-{key}.{suffix}", key
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), key
        assert isinstance(entry["bytes"], int) and entry["bytes"] > 0, key
