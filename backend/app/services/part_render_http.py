"""Loopback HTTP job server for one part-render attempt (spec §5.5, §5.6).

Serves the built render-worker page -- the HTML entry and exactly the files its
Vite manifest names -- the job description and the plate's G-code, and accepts
the page's PNGs and its manifest or error. Binds 127.0.0.1 only; every path
carries a one-time token; anything else is 404. Runs in the disposable render
child, which is synchronous, so this is stdlib ``http.server`` on a thread.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO

ENTRY = "index.html"
_CHUNK = 128 * 1024
_JSON_BYTES = 1024 * 1024
_DRAIN_BYTES = 16 * 1024 * 1024
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".wasm": "application/wasm",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class JobLimits:
    png_bytes: int
    total_bytes: int


@dataclass
class JobOutcome:
    manifest: dict | None = None
    error: dict | None = None
    pngs: dict[int, Path] = field(default_factory=dict)


def bundle_allowlist(bundle_dir: Path) -> frozenset[str]:
    """The HTML entry plus every file the build's Vite manifest names."""
    manifest = json.loads((bundle_dir / ".vite" / "manifest.json").read_text(encoding="utf-8"))
    files = {ENTRY}
    for chunk in manifest.values():
        files.add(chunk["file"])
        files.update(chunk.get("css", ()))
        files.update(chunk.get("assets", ()))
    return frozenset(files)


class JobServer:
    def __init__(
        self,
        *,
        bundle_dir: Path,
        job: dict,
        open_gcode: Callable[[], BinaryIO],
        gcode_bytes: int,
        out_dir: Path,
        limits: JobLimits,
    ) -> None:
        self._bundle_dir = bundle_dir.resolve()
        self._allow = bundle_allowlist(self._bundle_dir)
        self._job = json.dumps(job).encode()
        self._ids = {int(o["id"]) for o in job["objects"]}
        self._open_gcode = open_gcode
        self._gcode_bytes = gcode_bytes
        self._out_dir = out_dir
        self._limits = limits
        self._token = secrets.token_urlsafe(24)
        self._received = 0
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.outcome = JobOutcome()
        self.origin = ""

    def start(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        server.daemon_threads = True
        self._server = server
        self.origin = f"127.0.0.1:{server.server_address[1]}"
        self._thread = threading.Thread(target=server.serve_forever, name="part-render-http", daemon=True)
        self._thread.start()
        return f"http://{self.origin}/{self._token}/"

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BamDudePartRender"

            def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
                # Per-request lines would flood the child's bounded diagnostic buffer.
                return

            def _route(self) -> str | None:
                if self.headers.get("Host", "") != owner.origin:
                    return None
                prefix = f"/{owner._token}/"
                if not self.path.startswith(prefix):
                    return None
                return self.path[len(prefix) :].split("?", 1)[0] or ENTRY

            def _reply(self, status: HTTPStatus, body: bytes = b"", content_type: str = "text/plain") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                route = self._route()
                if route is None:
                    return self._reply(HTTPStatus.NOT_FOUND)
                if route == "job.json":
                    return self._reply(HTTPStatus.OK, owner._job, "application/json")
                if route == "plate.gcode":
                    return self._send_gcode()
                if route not in owner._allow:
                    return self._reply(HTTPStatus.NOT_FOUND)
                path = (owner._bundle_dir / route).resolve()
                if owner._bundle_dir not in path.parents or not path.is_file():
                    return self._reply(HTTPStatus.NOT_FOUND)
                self._reply(
                    HTTPStatus.OK, path.read_bytes(), _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
                )

            def _send_gcode(self) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(owner._gcode_bytes))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with owner._open_gcode() as stream:
                    while block := stream.read(_CHUNK):
                        self.wfile.write(block)

            def do_POST(self) -> None:  # noqa: N802 - stdlib name
                route = self._route()
                length = int(self.headers.get("Content-Length") or -1)
                if route is None or length < 0:
                    return self._reply(HTTPStatus.NOT_FOUND)
                if route.startswith("png/"):
                    return self._receive_png(route[4:], length)
                if route in ("manifest", "error"):
                    if length > _JSON_BYTES:
                        return self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, length)
                    if route == "manifest":
                        # One budget for every PNG plus the manifest (spec §5.4). The
                        # error stays outside it: it is how the page ends an attempt
                        # it cannot finish, and must land even with the budget spent.
                        with owner._lock:
                            over = owner._received + length > owner._limits.total_bytes
                            if not over:
                                owner._received += length
                        if over:
                            return self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, length)
                    try:
                        payload = json.loads(self.rfile.read(length))
                    except ValueError:
                        return self._reply(HTTPStatus.BAD_REQUEST)
                    if route == "manifest":
                        owner.outcome.manifest = payload
                    else:
                        owner.outcome.error = payload
                    owner._done.set()
                    return self._reply(HTTPStatus.NO_CONTENT)
                self._reply(HTTPStatus.NOT_FOUND)

            def _refuse(self, status: HTTPStatus, length: int) -> None:
                # Read (a bounded part of) the body first: closing on unread
                # data resets the connection, and the page would see a network
                # error instead of the status it has to act on.
                remaining = min(length, _DRAIN_BYTES)
                while remaining > 0 and (block := self.rfile.read(min(_CHUNK, remaining))):
                    remaining -= len(block)
                self._reply(status)

            def _receive_png(self, raw_id: str, length: int) -> None:
                if not raw_id.isdigit() or int(raw_id) not in owner._ids:
                    return self._refuse(HTTPStatus.NOT_FOUND, length)
                with owner._lock:
                    over = length > owner._limits.png_bytes or owner._received + length > owner._limits.total_bytes
                    if not over:
                        owner._received += length
                if over:
                    return self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, length)
                object_id = int(raw_id)
                target = owner._out_dir / f"{object_id}.png"
                target.write_bytes(self.rfile.read(length))
                owner.outcome.pngs[object_id] = target
                self._reply(HTTPStatus.NO_CONTENT)

        return Handler
