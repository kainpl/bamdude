#!/bin/sh
# CI (spec Q2, plan A7): a 32-bit armhf BamDude keeps its STL pictures without any CAD component. On a real armhf
# userland (Debian trixie under QEMU), Debian's own numpy, matplotlib and Pillow plus the trimesh requirements.txt
# names render a cube STL through the preview's PNG child — the module, request line and environment a preview
# uses — and the picture must decode and show something. The CAD half (step_preview_armv7_smoke.sh) runs with no
# network at all: no CAD build, nothing downloaded.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-numpy python3-matplotlib python3-pil python3-psutil python3-pip ca-certificates >/dev/null
python3 -m pip install -q --root-user-action=ignore --break-system-packages --no-deps "$(grep -i '^trimesh' requirements.txt | head -1)"
python3 -c "import platform, struct, sys; print(platform.machine(), struct.calcsize('P') * 8, sys.version.split()[0])"
python3 - <<'PY'
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from backend.app.step_runtime import png_drawn

root = Path(tempfile.mkdtemp(prefix="stl-smoke-"))
v = [(x, y, z) for x in (0, 20) for y in (0, 20) for z in (0, 20)]
faces = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
rows = ["solid cube"]
for a, b, c, d in faces:
    for tri in ((a, b, c), (a, c, d)):
        rows.append(" facet normal 0 0 0\n  outer loop")
        rows += [f"   vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in tri]
        rows.append("  endloop\n endfacet")
rows.append("endsolid cube")
(root / "mesh.stl").write_text("\n".join(rows) + "\n", encoding="ascii")
request = {"root": str(root), "operation": "mesh", "kind": "stl", "deadline": time.monotonic_ns() + 600 * 10**9}
env = dict(os.environ, MPLCONFIGDIR=str(root / "mpl"), OMP_NUM_THREADS="1")
p = subprocess.run([sys.executable, "-m", "backend.app.preview_render"], input=json.dumps(request) + "\n",
                   capture_output=True, text=True, timeout=900, env=env)
result = json.loads((root / "result.json").read_text(encoding="utf-8"))
ok = result.get("outcome") == "ok" and png_drawn(root / "preview.png")
print(json.dumps({"stl_outcome": result.get("outcome"), "png_drawn": ok, "rc": p.returncode}))
sys.exit(0 if ok else 1)
PY
