"""The tracked part-render bundle (spec §5.5): it matches its manifest, its guarded dependency graph is recorded, and it keeps its bytes."""

import json
import subprocess
from pathlib import Path

from backend.app.services import part_render_node

ALLOWED_MODULES = ("src/part-render/", "src/lib/gcodeToolpath.ts", "src/lib/vendor/toolpathRenderer.js")


def manifest() -> dict:
    return json.loads(part_render_node.BUNDLE_MANIFEST.read_text(encoding="utf-8"))


def test_bundle_matches_its_manifest():
    m = manifest()
    assert m["file"] == part_render_node.BUNDLE.name
    assert m["bytes"] == part_render_node.BUNDLE.stat().st_size
    assert part_render_node.verify_bundle() == m["sha256"]


def test_manifest_records_a_guarded_dependency_graph():
    m = manifest()
    assert m["imports"] == ["node:crypto", "node:zlib"]
    assert m["modules"] and all(mod.startswith(ALLOWED_MODULES) for mod in m["modules"]), m["modules"]


def test_bundle_is_not_eol_converted():
    root = Path(__file__).resolve().parents[3]
    for name in ("part-render.mjs", "manifest.json"):
        out = subprocess.run(
            ["git", "check-attr", "text", f"backend/app/data/part_render/{name}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out.strip().endswith("text: unset"), out
