"""An optional client-package failure must not abort install or trigger update rollback."""

import re
import subprocess
from pathlib import Path

import pytest

from backend.tests.unit.test_update_script_repairs_loop_flag import BASH

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


@pytest.mark.parametrize("route", ["install-macos", "install-linux", "update"])
@pytest.mark.parametrize("failure", ["ci", "build", "frontend", "none"])
def test_optional_package_failure_continues_but_frontend_failure_does_not(tmp_path, route, failure):
    install = tmp_path / "bam dude"
    (install / "frontend").mkdir(parents=True)
    (install / "frontend/package.json").write_text("{}")
    (install / "integrations/mcp-adapter").mkdir(parents=True)
    (install / "integrations/mcp-adapter/package.json").write_text("{}")
    if route == "update":
        text = (ROOT / "install/update.sh").read_text(encoding="utf-8")
        start = text.index('if [ -f "$FRONTEND_DIR/package.json" ]; then')
        steps = text[start : text.index("# While the service is stopped", start)]
    else:
        text = (ROOT / "install/install.sh").read_text(encoding="utf-8")
        match = re.search(r"^build_frontend\(\) \{\n.*?^\}\n", text, re.S | re.M)
        assert match
        steps = match.group(0) + "\nbuild_frontend\n"
    # Run the real code under the updater's ERR-trap/errexit conditions. No real npm or sudo.
    script = r"""
set -Eeuo pipefail
trap 'echo ROLLBACK >&2; exit 37' ERR
INSTALL_PATH="$1"; INSTALL_DIR="$1"; FRONTEND_DIR="$1/frontend"; FRONTEND_BUILD=1
OS_TYPE="$2"; SERVICE_USER=fixture; FAIL="$3"
log() { echo "$*"; }; log_info() { log "$@"; }; log_success() { log "$@"; }
warn() { echo "WARNING $*"; }; log_warn() { warn "$@"; }
check_node_version() { return 0; }
sudo() { shift 3; "$@"; }; as_owner() { "$@"; }
npm() {
  if [[ "$PWD" == */mcp-adapter ]]; then
    echo "PACKAGE $*"
    if [[ "$1" == ci && "$FAIL" == ci ]]; then return 1; fi
    if [[ "$1" == run && "$FAIL" == build ]]; then return 1; fi
  else
    echo "FRONTEND $*"
    if [[ "$FAIL" == frontend ]]; then return 1; fi
  fi
}
"""
    script += steps + "\necho CONTINUED\n"
    script_path = tmp_path / "probe.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [BASH, str(script_path), str(install), route.removeprefix("install-"), failure],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if failure == "frontend":
        assert result.returncode == 37, result.stdout + result.stderr
        assert "CONTINUED" not in result.stdout
        assert "PACKAGE" not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ROLLBACK" not in result.stderr
        assert "CONTINUED" in result.stdout
        assert ("WARNING" in result.stdout) == (failure != "none")
        assert ("AI client packages built" in result.stdout) == (failure == "none")
        if failure == "ci":
            assert "PACKAGE run build" not in result.stdout
