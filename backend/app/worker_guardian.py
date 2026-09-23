"""Independent parent-death watcher for allowlisted local workers.

The owner keeps stdin open. EOF kills this POSIX process group (including the
native-hung child); on Windows the owner also holds a kill-on-close Job Object.
Bootstrap is withheld until containment has been attached by the owner.
"""

import base64
import binascii
import json
import os
import signal
import subprocess
import sys
import threading

_PREVIEW_MODULES = {
    "backend.app.preview_service",
    "backend.app.preview_render",
    "backend.app.analysis_service",
    "backend.app.analysis_child",
}
_CAMERA_MODULE = "backend.app.camera_worker"
_PREVIEW_BOOTSTRAP_LIMIT = 16384
_CAMERA_BOOTSTRAP_LIMIT = 65536


def _child_bootstrap(line: bytes) -> tuple[str, bytes]:
    """Decode only the two known wire formats, without importing any worker."""
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("invalid worker bootstrap")
    module = data.pop("module")
    if module in _PREVIEW_MODULES:
        if len(line) > _PREVIEW_BOOTSTRAP_LIMIT:
            raise ValueError("preview bootstrap too large")
        return module, json.dumps(data).encode() + b"\n"
    if module == _CAMERA_MODULE:
        if set(data) != {"camera_frame"} or not isinstance(data["camera_frame"], str):
            raise ValueError("invalid camera bootstrap envelope")
        try:
            frame = base64.b64decode(data["camera_frame"], validate=True)
        except binascii.Error as exc:
            raise ValueError("invalid camera bootstrap frame") from exc
        if len(frame) < 4 or len(frame) > _CAMERA_BOOTSTRAP_LIMIT + 4:
            raise ValueError("camera bootstrap size")
        if int.from_bytes(frame[:4], "big") != len(frame) - 4:
            raise ValueError("camera bootstrap length")
        return module, frame
    raise ValueError("worker module is not allowed")


def main():
    bootstrap = bytearray()
    while len(bootstrap) <= 98304 and not bootstrap.endswith(b"\n"):
        block = os.read(0, 1)
        if not block:
            return 2
        bootstrap.extend(block)
    if len(bootstrap) > 98304 or not bootstrap.endswith(b"\n"):
        return 2
    try:
        module, child_bootstrap = _child_bootstrap(bytes(bootstrap))
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return 2
    child = subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def owner_died():
        while chunk := os.read(0, 4096):
            if module == "backend.app.analysis_child":
                try:
                    child.stdin.write(chunk)
                    child.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
        if os.name != "nt":
            os.killpg(os.getpgrp(), signal.SIGKILL)
        else:
            child.kill()  # main Job Object is the tree backstop

    child.stdin.write(child_bootstrap)
    child.stdin.flush()
    if module != "backend.app.analysis_child":
        child.stdin.close()
    threading.Thread(target=owner_died, daemon=True).start()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
