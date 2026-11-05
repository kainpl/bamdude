"""HTTP to one configured server, and nothing else (spec r2 §12).

- TLS is verified, with an explicit CA file for a self-signed LAN server; there is no switch to turn it off.
- No redirect is followed, to any origin: a 3xx is a refusal the user fixes by entering the final URL.
- Credentials go only to the configured authority. Responses are bounded; an HTML or other non-JSON body is a
  protocol error, never shown. An error's text is the server's ``detail.message`` at most -- never a raw body,
  a header, the key or a stack.
- Every call has a timeout. An upload streams a file in chunks and can be cancelled between chunks.
"""

from __future__ import annotations

import http.client
import json
import ssl
from collections.abc import Callable, Iterable
from urllib.parse import urlencode, urlsplit

from orca_bamdude.config import Connection

USER_AGENT = "OrcaBamDude"
MAX_JSON_RESPONSE = 4 * 1024 * 1024
MAX_MESSAGE = 500
UPLOAD_CHUNK = 256 * 1024


class TransportError(Exception):
    """The request did not get a usable answer. ``kind`` is stable: timeout, tls, connect, redirect, protocol,
    too_large, cancelled."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class HttpError(Exception):
    """The server answered with a refusal: its status, its stable code and its (already translated) sentence."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(f"{status} {code}")
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _ssl_context(ca_file: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _retry_after(response: http.client.HTTPResponse) -> float | None:
    value = response.getheader("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _read_bounded(response: http.client.HTTPResponse, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise TransportError("too_large")
    return data


def _refusal(status: int, payload) -> HttpError:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        code = str(detail.get("error") or "http_error")[:64]
        message = str(detail.get("message") or "")[:MAX_MESSAGE]
    else:
        code, message = "http_error", (detail[:MAX_MESSAGE] if isinstance(detail, str) else "")
    return HttpError(status, code, message)


class Transport:
    def __init__(self, connection: Connection, *, timeout: float = 10.0):
        self.connection = connection
        self.timeout = timeout
        parts = urlsplit(connection.base_url)
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port
        self._base_path = parts.path
        self._context = _ssl_context(connection.ca_file) if parts.scheme == "https" else None

    # ------------------------------------------------------------------ plumbing

    def _open(self, timeout: float | None = None) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout or self.timeout, context=self._context
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout or self.timeout)

    def _target(self, path: str, query: dict | Iterable | None) -> str:
        if not path.startswith("/api/v1/"):
            raise ValueError("only API paths are requested")
        target = self._base_path + path
        if query:
            target += "?" + urlencode(query, doseq=True)
        return target

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(self.connection.extra_headers)
        headers.update({"User-Agent": USER_AGENT, "X-API-Key": self.connection.api_key, "Accept": "application/json"})
        headers.update(extra or {})
        return headers

    def _send(self, conn: http.client.HTTPConnection, method: str, target: str, body, headers: dict):
        try:
            conn.request(method, target, body=body, headers=headers)
            return conn.getresponse()
        except ssl.SSLError as exc:
            raise TransportError("tls") from exc
        except TimeoutError as exc:
            raise TransportError("timeout") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise TransportError("connect") from exc

    def _json(self, response: http.client.HTTPResponse) -> tuple[int, object]:
        status = response.status
        if 300 <= status < 400:
            raise TransportError("redirect")
        try:
            raw = _read_bounded(response, MAX_JSON_RESPONSE)
        except TimeoutError as exc:
            raise TransportError("timeout") from exc
        content_type = (response.getheader("Content-Type") or "").lower()
        payload = None
        if raw:
            if "json" not in content_type:
                if status >= 400:
                    raise HttpError(status, "http_error", "", _retry_after(response))
                raise TransportError("protocol")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise TransportError("protocol") from exc
        if status >= 400:
            refusal = _refusal(status, payload)
            refusal.retry_after = _retry_after(response)
            raise refusal
        return status, payload

    # ------------------------------------------------------------------ requests

    def request_json(self, method: str, path: str, *, query=None, body: dict | None = None) -> tuple[int, object]:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        headers = self._headers({"Content-Type": "application/json"} if encoded is not None else None)
        conn = self._open()
        try:
            response = self._send(conn, method, self._target(path, query), encoded, headers)
            return self._json(response)
        finally:
            conn.close()

    def upload_file(
        self,
        path: str,
        file_path: str,
        size: int,
        *,
        query=None,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: float = 300.0,
    ) -> tuple[int, object]:
        """Stream a file as the raw body. Cancelling between chunks closes the connection; the server discards it."""
        headers = self._headers({"Content-Type": "application/octet-stream", "Content-Length": str(size)})
        conn = self._open(timeout)
        try:
            try:
                conn.putrequest("POST", self._target(path, query), skip_accept_encoding=True)
                for name, value in headers.items():
                    conn.putheader(name, value)
                conn.endheaders()
                sent = 0
                with open(file_path, "rb") as handle:
                    while True:
                        if cancelled is not None and cancelled():
                            raise TransportError("cancelled")
                        chunk = handle.read(UPLOAD_CHUNK)
                        if not chunk:
                            break
                        conn.send(chunk)
                        sent += len(chunk)
                        if progress is not None:
                            progress(sent, size)
                response = conn.getresponse()
            except TransportError:
                raise
            except ssl.SSLError as exc:
                raise TransportError("tls") from exc
            except TimeoutError as exc:
                raise TransportError("timeout") from exc
            except (ConnectionError, OSError, http.client.HTTPException) as exc:
                raise TransportError("connect") from exc
            return self._json(response)
        finally:
            conn.close()

    def open_stream(self, path: str, *, headers: dict | None = None, query=None, timeout: float = 15.0):
        """An open response for a camera relay; the caller reads and closes it. Never follows a redirect."""
        conn = self._open(timeout)
        response = self._send(conn, "GET", self._target(path, query), None, self._headers(headers or {}))
        if 300 <= response.status < 400:
            conn.close()
            raise TransportError("redirect")
        if response.status >= 400:
            try:
                self._json(response)
            finally:
                conn.close()
        return conn, response
