"""Independent parent-death watcher, never imports the renderer or application.

The owner keeps stdin open. EOF kills this POSIX process group (including the
native-hung child); on Windows the owner also holds a kill-on-close Job Object.
Bootstrap is withheld until containment has been attached by the owner.
"""

import json
import os
import signal
import subprocess
import sys
import threading


def main():
    bootstrap = sys.stdin.buffer.readline(16385)
    if len(bootstrap) > 16384:
        return 2
    data = json.loads(bootstrap)
    module = data.pop("module")
    if module not in {"backend.app.preview_service", "backend.app.preview_render"}:
        return 2
    child = subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def owner_died():
        while os.read(0, 1):  # no buffered-reader lock at interpreter shutdown
            pass
        if os.name != "nt":
            os.killpg(os.getpgrp(), signal.SIGKILL)
        else:
            child.kill()  # main Job Object is the tree backstop

    threading.Thread(target=owner_died, daemon=True).start()
    child.stdin.write(json.dumps(data).encode() + b"\n")
    child.stdin.close()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
