#!/bin/sh
# CI (part-render-node-lines-spec D8): the part-render runtime on a real 32-bit armhf userland -- QEMU linux/arm/v7,
# Debian trixie. Provisions with the AUTOMATIC platform key (proves D4) and renders the golden fixtures.
# The runtime goes OUTSIDE the checkout: a developer's checkout mounted here holds the host's own runtime/.
set -eu
export DEBIAN_FRONTEND=noninteractive
RUNTIME=/tmp/part-render-runtime
REPORT=${REPORT:-/tmp/probe-report.json}
apt-get update -qq
apt-get install -y -qq python3 python3-psutil python3-pil ca-certificates >/dev/null
python3 -c "from backend.app.services.render_runtime import platform_key as k; key = k(); print(key); assert key == 'linux-armv7l', key"
python3 -m backend.app.services.render_runtime provision --runtime "$RUNTIME"
NODE=$(python3 -c "from pathlib import Path; from backend.app.services.render_runtime import locate_in; print(locate_in(Path('$RUNTIME')).executable)")
"$NODE" --version
python3 -m backend.app.part_render_probe render --node "$NODE" --report "$REPORT"
