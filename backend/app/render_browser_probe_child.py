"""Diagnostic child for the render-browser containment probe (plan task 10).

Started by worker_guardian with a bootstrap line {"cmd": [...]}; starts the
browser and waits. The owner-death scenario kills the owner and expects the
guardian's EOF path / the Job Object to take this whole tree down.

Only the provisioned browser runs: worker_guardian launches allowlisted
modules alone, and a child that ran any argv it was handed would turn that
allowlist into "run anything".
"""

import json
import subprocess
import sys
from pathlib import Path

from backend.app.services.render_browser import locate

APP_DIR = Path(__file__).resolve().parents[2]


def main() -> int:
    cmd = json.loads(sys.stdin.readline())["cmd"]
    located = locate(APP_DIR)
    if located is None or not cmd or Path(cmd[0]).resolve() != located.executable.resolve():
        print("render-browser probe child: refusing a command that is not the provisioned browser", file=sys.stderr)
        return 2
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
