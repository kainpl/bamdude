"""Exercise a fresh production container over HTTP, using only the standard library.

Run inside the disposable container via ``docker exec -i ... python - < this_file``.
No application imports, test fixtures, credentials on disk, or access to a real farm.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:8000"


def request(path, *, method="GET", body=None, token=None, status=200, json_response=True):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        if response.status != status:
            # Never print response bodies: setup/login responses contain credentials.
            raise RuntimeError(f"{method} {path}: HTTP {response.status}, expected {status}")
        payload = response.read()
        content_type = response.headers.get_content_type()
    if not json_response:
        return payload, content_type
    if content_type != "application/json":
        raise RuntimeError(f"{method} {path}: expected JSON")
    try:
        return json.loads(payload)
    except ValueError:
        raise RuntimeError(f"{method} {path}: invalid JSON") from None


def wait_ready():
    deadline = time.monotonic() + 90
    while True:
        try:
            if request("/health").get("status") == "healthy":
                return
        except (OSError, URLError, RuntimeError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("production HTTP server did not become healthy")
        time.sleep(1)


def check_static():
    page, content_type = request("/", json_response=False)
    assert content_type == "text/html", "index is not HTML"
    assets = re.findall(rb'(?:src|href)="(/assets/[^"?#]+\.(?:js|css))"', page)
    assert any(asset.endswith(b".js") for asset in assets), "index has no built JavaScript"
    for asset in set(assets):
        payload, content_type = request(asset.decode("ascii"), json_response=False)
        assert payload and content_type != "text/html", "asset served the SPA fallback"
        expected = {"text/css"} if asset.endswith(b".css") else {"application/javascript", "text/javascript"}
        assert content_type in expected, "asset has the wrong content type"


def smoke():
    wait_ready()
    check_static()
    assert request("/api/v1/auth/status")["requires_setup"] is True, "requires a fresh disposable database"
    password = "Aa1" + secrets.token_urlsafe(24)
    username = "ci_http_" + secrets.token_hex(6)
    setup = request("/api/v1/auth/setup", method="POST", body={"admin_username": username, "admin_password": password})
    assert setup["admin_created"] and setup["access_token"], "initial admin was not created"
    assert request("/api/v1/auth/status")["requires_setup"] is False, "setup did not persist"
    login = request("/api/v1/auth/login", method="POST", body={"username": username, "password": password})
    token = login["access_token"]
    assert token and login["user"]["username"] == username, "login failed"
    request("/api/v1/library/folders", status=401)
    name = "CI HTTP folder " + secrets.token_hex(6)
    folder = request("/api/v1/library/folders", method="POST", body={"name": name}, token=token)
    path = f"/api/v1/library/folders/{int(folder['id'])}"
    assert request(path, token=token)["name"] == name, "folder write/read failed"
    request(path, method="PUT", body={"name": name + " renamed"}, token=token)
    assert request(path, token=token)["name"] == name + " renamed", "folder update did not persist"
    assert request(path, method="DELETE", token=token)["status"] == "success", "folder delete failed"
    request(path, token=token, status=404)
    print("Production HTTP smoke: health, built assets, setup, login, auth gate, folder CRUD OK")


if __name__ == "__main__":
    smoke()
