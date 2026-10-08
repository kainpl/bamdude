"""Spec §8.5: whatever reaches the pixels or the choice of instances is pinned together with RENDERER_VERSION."""

import hashlib
from pathlib import Path

from backend.app.services.part_render_protocol import RENDERER_VERSION

ROOT = Path(__file__).resolve().parents[3]
RENDER_INPUTS = (
    "frontend/src/part-render/entry.ts",
    "frontend/src/part-render/io.ts",
    "frontend/src/part-render/job.ts",
    "frontend/src/part-render/png.ts",
    "frontend/src/part-render/protocol.ts",
    "frontend/src/part-render/raster.ts",
    "frontend/src/part-render/select.ts",
    "frontend/src/lib/gcodeToolpath.ts",
    "frontend/src/lib/vendor/toolpathRenderer.js",
    "backend/app/services/part_render_fallback.py",
    "backend/app/services/part_render_protocol.py",
    "backend/app/part_render.py",
)
# Changed a file above? Decide first whether RENDERER_VERSION goes up (spec §8.5), then re-pin with
#   python -c "from backend.tests.unit.test_renderer_inputs_digest import digest; print(digest())"
PINNED = {2: "b8655dc1ab4e92f7a954c201694ea1898424ba7bbbf1d08c9e29380492315154"}


def digest() -> str:
    h = hashlib.sha256()
    for rel in RENDER_INPUTS:
        h.update(rel.encode() + b"\0")
        h.update((ROOT / rel).read_bytes().replace(b"\r\n", b"\n") + b"\0")  # the same on a Windows checkout
    return h.hexdigest()


def test_the_render_inputs_are_pinned_with_their_version():
    assert digest() == PINNED[RENDERER_VERSION], "render inputs changed: decide on RENDERER_VERSION, then re-pin"
