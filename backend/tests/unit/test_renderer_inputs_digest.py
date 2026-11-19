"""Spec §8.5: whatever reaches the pixels or the choice of instances is pinned together with RENDERER_VERSION."""

import hashlib
from pathlib import Path

from backend.app.services.part_render_protocol import RENDERER_VERSION

ROOT = Path(__file__).resolve().parents[3]
RENDER_INPUTS = (
    "frontend/src/part-render/colour.ts",  # the colour a filament is painted in (spec §34)
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
    # which instances a plate has and which are chosen (spec §5.4, §8.5; final review M11)
    "backend/app/services/part_names.py",
    "backend/app/services/threemf_parser_core.py",
)
# Changed a file above? Decide first whether RENDERER_VERSION goes up (spec §8.5), then re-pin with
#   python -c "from backend.tests.unit.test_renderer_inputs_digest import digest; print(digest())"
# 3: a dark filament raised to its lightness floor (spec §34). Re-pinned under 3 in E7 (2026-10-12, owner): a single
# object's bbox is found by name beside the wipe tower and proven by its first layer (spec §5.3, condition 3), so such
# plates now get `model` instead of `top_mask` -- the feature is unreleased, so no renderer-3 result exists to replace.
# Re-pinned under 3 after G04 (2026-11-19): threemf_parser_core's project-settings parse now takes the nozzle
# temperature of the printed plate's first used filament. The renderer reads only `discover_plate_objects` from that
# file, which does not reach the changed method, so no pixel moves and the version stays.
PINNED = {3: "8f22a0f30381f2a48c91867290faa127afd227dbeb9589eed46f8525fe10d22e"}


def digest() -> str:
    h = hashlib.sha256()
    for rel in RENDER_INPUTS:
        h.update(rel.encode() + b"\0")
        h.update((ROOT / rel).read_bytes().replace(b"\r\n", b"\n") + b"\0")  # the same on a Windows checkout
    return h.hexdigest()


def test_the_render_inputs_are_pinned_with_their_version():
    assert digest() == PINNED[RENDERER_VERSION], "render inputs changed: decide on RENDERER_VERSION, then re-pin"
