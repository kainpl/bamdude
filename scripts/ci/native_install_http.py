"""The HTTP half of the native install acceptance (spec stage C R12): the INSTALLED service, on its scenario's own
port, turns a real STEP into a library thumbnail (an STL as the control) or fails it with cad_unavailable, and the
System page names the STEP component's state.

The flows are the Windows acceptance's (windows_installer_http), pointed at another port. Standard library only;
never prints a response body or a credential.

Modes:
  health               the service answers healthy
  step-ok              a STEP's background job succeeds and its PNG is served; an STL gets its PNG on upload
  step-cad-gone        the STEP's job fails with cad_unavailable while an STL still gets its PNG
  step-preview-state   prints the System page's step_preview as one JSON line
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docker_http_smoke  # noqa: E402
import windows_installer_http as flows  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="native_install_http.py")
    ap.add_argument("mode", choices=["health", "step-ok", "step-cad-gone", "step-preview-state"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--state", type=Path, required=True)
    a = ap.parse_args(argv)
    base = f"http://127.0.0.1:{a.port}"
    docker_http_smoke.BASE_URL = base  # request() and wait_ready() read it
    flows.BASE_URL = base  # upload() reads its own copy
    if a.mode in ("health", "step-ok", "step-cad-gone"):
        return flows.main([a.mode, "--state", str(a.state)])
    docker_http_smoke.wait_ready()
    info = docker_http_smoke.request("/api/v1/system/info", token=flows.token(a.state))
    print(json.dumps(info.get("step_preview"), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError) as exc:
        print(f"http: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
