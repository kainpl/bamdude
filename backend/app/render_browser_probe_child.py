"""Diagnostic child for the render-browser containment probe (plan task 10).

Started by worker_guardian with a bootstrap line {"cmd": [...]}; starts the
browser and waits. The owner-death scenario kills the owner and expects the
guardian's EOF path / the Job Object to take this whole tree down.
"""

import json
import subprocess
import sys


def main() -> int:
    cmd = json.loads(sys.stdin.readline())["cmd"]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
