"""A stubbed socket layer that speaks HTTP/1.1 — the transport's REAL client runs over it.

``WireSockets(handler)`` stands where the operating system's sockets are: every connection the
transport opens is recorded (the address it was opened to, the TLS server name asked for), each
request written to it is parsed, and ``handler(httpx.Request)`` answers it — with an
``httpx.Response`` (serialised here) or with raw bytes written to the wire as they are (a
compressed body, a broken answer). No network is touched.
"""

from __future__ import annotations

import httpcore
import httpx


def serialise(response: httpx.Response) -> bytes:
    body = response.read()
    head = [f"HTTP/1.1 {response.status_code} X".encode()]
    for name, value in response.headers.raw:
        if name.lower() not in (b"content-length", b"transfer-encoding"):
            head.append(name + b": " + value)
    head.append(b"Content-Length: " + str(len(body)).encode())
    return b"\r\n".join(head) + b"\r\n\r\n" + body


class WireStream(httpcore.AsyncNetworkStream):
    def __init__(self, sockets: WireSockets, address: tuple[str, int]):
        self.sockets, self.address = sockets, address
        self.server_hostname: str | None = None
        self.requests: list[httpx.Request] = []
        self.closed = False
        self._inbox = b""
        self._pending = b""

    async def read(self, max_bytes, timeout=None):
        data, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self._inbox += bytes(buffer)
        while b"\r\n\r\n" in self._inbox:
            head, _, rest = self._inbox.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            method, target, _version = lines[0].split(b" ", 2)
            headers = [tuple(part.strip() for part in line.split(b":", 1)) for line in lines[1:]]
            length = next((int(value) for name, value in headers if name.lower() == b"content-length"), 0)
            if len(rest) < length:
                return
            self._inbox = rest[length:]
            host = next(value for name, value in headers if name.lower() == b"host").decode()
            scheme = "https" if self.server_hostname is not None else "http"
            request = httpx.Request(
                method.decode(), f"{scheme}://{host}{target.decode()}", headers=headers, content=rest[:length]
            )
            self.requests.append(request)
            answer = self.sockets.handler(request)
            self._pending += answer if isinstance(answer, bytes) else serialise(answer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sockets.tls_contexts.append(ssl_context)
        self.server_hostname = server_hostname
        if server_hostname in self.sockets.certificate_mismatch:
            raise httpcore.ConnectError(f"certificate verify failed: hostname mismatch for {server_hostname}")
        return self

    def get_extra_info(self, info):
        return None


class WireSockets(httpcore.AsyncNetworkBackend):
    def __init__(self, handler):
        self.handler = handler
        self.opened: list[WireStream] = []
        self.refuse: set[str] = set()  # addresses whose connect fails
        self.certificate_mismatch: set[str] = set()
        self.tls_contexts: list = []
        self.attempts: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.attempts.append((host, port))
        if host in self.refuse:
            raise httpcore.ConnectError("connection refused")
        stream = WireStream(self, (host, port))
        self.opened.append(stream)
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("no unix socket is ever asked for")

    async def sleep(self, seconds):
        pass

    def addresses(self) -> list[tuple[str, int]]:
        return [stream.address for stream in self.opened]

    def requests(self) -> list[httpx.Request]:
        return [request for stream in self.opened for request in stream.requests]
