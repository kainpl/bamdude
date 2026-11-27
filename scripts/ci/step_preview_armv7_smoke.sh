#!/bin/sh
# CI (spec Q2, plan A7): 32-bit armhf has no STEP-preview CAD build. The container runs with no network at all, so
# the helper must answer "unsupported" (exit 3) to select, install and verify without ever reaching for a download —
# and verify again once pip is gone, as in the final Docker image, where an armv7 build relies on that exit 3.
set -u
check() {
    python -m backend.app.step_runtime "$1" --app-dir /tmp/app
    rc=$?
    echo "$1 ($2) -> $rc"
    [ "$rc" -eq 3 ] || exit 1
}
for cmd in select install verify; do
    check "$cmd" "with pip"
done
python -m pip uninstall --yes --root-user-action=ignore pip >/dev/null 2>&1
python -c "import pip" 2>/dev/null && { echo "pip still importable"; exit 1; }
check verify "without pip"
test ! -e /tmp/app/runtime || { echo "unsupported install left files"; exit 1; }
