"""The HTTP half of the Windows installer acceptance (spec stage B R13): the INSTALLED service — LocalSystem, its
DATA_DIR under ProgramData — turns a real STEP into a library thumbnail, and an STL as the control.

Standard library only; reuses docker_http_smoke's request/wait_ready (127.0.0.1:8000). Never prints a response body
or a credential: the admin created on a fresh database is kept for later calls in --state, a file under the
runner's temp dir that the acceptance removes with its reset.

Modes:
  health           the service answers healthy
  step-ok          a STEP's background job succeeds and its PNG is served; an STL gets its PNG on upload
  step-cad-gone    the STEP's job fails with cad_unavailable while an STL still gets its PNG (the app lives on)
"""

from __future__ import annotations

import argparse
import json
import secrets
import struct
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docker_http_smoke import BASE_URL, request, wait_ready  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STEP = ROOT / "backend" / "app" / "data" / "step_probe" / "box_mm.step"
JOB_SECONDS = 600


def token(state: Path) -> str:
    """A signed-in admin: created on a fresh database (its credentials kept in ``state``), else signed in again."""
    if request("/api/v1/auth/status")["requires_setup"]:
        creds = {"username": "ci_win_" + secrets.token_hex(6), "password": "Aa1" + secrets.token_urlsafe(24)}
        setup = request(
            "/api/v1/auth/setup",
            method="POST",
            body={"admin_username": creds["username"], "admin_password": creds["password"]},
        )
        assert setup["admin_created"], "the initial admin was not created"
        state.write_text(json.dumps(creds), encoding="utf-8")
    creds = json.loads(state.read_text(encoding="utf-8"))
    return request("/api/v1/auth/login", method="POST", body=creds)["access_token"]


def upload(name: str, content: bytes, bearer: str) -> dict:
    boundary = uuid.uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = Request(
        BASE_URL + "/api/v1/library/files",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {bearer}"},
    )
    try:
        with urlopen(req, timeout=120) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"upload {name}: HTTP {exc.code}") from None


def thumbnail_png(file_id: int, bearer: str) -> bool:
    payload, _type = request(f"/api/v1/library/files/{file_id}/thumbnail", token=bearer, json_response=False)
    return payload[:8] == b"\x89PNG\r\n\x1a\n"


def unique_step() -> bytes:
    """The probe STEP with a comment of its own: the library dedupes by content, and every run needs a new job."""
    text = STEP.read_bytes()
    marker = b"ISO-10303-21;"
    assert text.startswith(marker), "unexpected STEP header"
    return marker + f"\n/* acceptance {uuid.uuid4().hex} */".encode() + text[len(marker) :]


def unique_stl() -> bytes:
    offset = secrets.randbelow(1000) / 10.0
    tris = [
        ((0, 0, 1), (offset, 0, 0), (offset + 10, 0, 0), (offset, 10, 0)),
        ((0, 0, 1), (offset + 10, 0, 0), (offset + 10, 10, 0), (offset, 10, 0)),
        ((0, 0, -1), (offset, 0, 5), (offset, 10, 5), (offset + 10, 0, 5)),
    ]
    out = b"\0" * 80 + struct.pack("<I", len(tris))
    for normal, a, b, c in tris:
        out += struct.pack("<12fH", *normal, *a, *b, *c, 0)
    return out


def step_job(bearer: str) -> tuple[str | None, str | None, int]:
    body = upload(f"acceptance-{uuid.uuid4().hex[:8]}.step", unique_step(), bearer)
    job = body.get("thumbnail_job") or {}
    assert job.get("receipt_id"), f"no thumbnail job was queued ({job.get('error_code')})"
    deadline = time.monotonic() + JOB_SECONDS
    while time.monotonic() < deadline:
        status = request(
            f"/api/v1/library/thumbnail-jobs?epoch={job['jobs_epoch']}&ids={job['receipt_id']}", token=bearer
        )
        receipt = (status.get("receipts") or [{}])[0]
        if receipt.get("state") in {"succeeded", "failed", "canceled"}:
            return receipt["state"], receipt.get("error_code"), body["id"]
        time.sleep(2)
    raise RuntimeError("the STEP job did not end in time")


def stl_png(bearer: str) -> bool:
    body = upload(f"acceptance-{uuid.uuid4().hex[:8]}.stl", unique_stl(), bearer)
    return bool(body.get("thumbnail_path")) and thumbnail_png(body["id"], bearer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="windows_installer_http.py")
    ap.add_argument("mode", choices=["health", "step-ok", "step-cad-gone"])
    ap.add_argument("--state", type=Path, required=True)
    a = ap.parse_args(argv)
    wait_ready()
    if a.mode == "health":
        print("http: healthy")
        return 0
    bearer = token(a.state)
    state, error, file_id = step_job(bearer)
    if a.mode == "step-ok":
        assert (state, error) == ("succeeded", None), f"the STEP job ended {state} ({error})"
        assert thumbnail_png(file_id, bearer), "the STEP thumbnail is not a PNG"
    else:
        assert (state, error) == ("failed", "cad_unavailable"), f"the STEP job ended {state} ({error})"
    assert stl_png(bearer), "the STL control got no PNG"
    print(f"http: {a.mode} ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError) as exc:
        print(f"http: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
