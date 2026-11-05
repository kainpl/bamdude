"""The transport against a real local HTTP server: what is sent, what is refused, what is never followed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from orca_bamdude.config import Connection
from orca_bamdude.transport import MAX_JSON_RESPONSE, HttpError, Transport, TransportError, _ssl_context


class Server:
    def __init__(self, routes):
        self.routes = routes
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.seen.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
                )
                status, headers, payload = outer.routes(self.command, self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda request, address: None  # a client that hung up on purpose
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _json(status, payload, **headers):
    return status, {"Content-Type": "application/json", **headers}, json.dumps(payload).encode()


@pytest.fixture
def serve():
    servers = []

    def start(routes, **conn):
        server = Server(routes)
        servers.append(server)
        connection = Connection.build(server.url + "/base", "bd_test_key", **conn)
        return server, Transport(connection, timeout=5)

    yield start
    for server in servers:
        server.close()


def test_a_request_carries_the_key_and_the_proxy_headers_to_the_base_path(serve):
    server, transport = serve(lambda m, p: _json(200, {"ok": True}), extra_headers=[("CF-Access-Client-Id", "abc")])

    status, body = transport.request_json("GET", "/api/v1/connector/capabilities")

    assert (status, body) == (200, {"ok": True})
    seen = server.seen[0]
    assert seen["path"] == "/base/api/v1/connector/capabilities"
    assert seen["headers"]["X-API-Key"] == "bd_test_key"
    assert seen["headers"]["CF-Access-Client-Id"] == "abc"


def test_a_redirect_is_never_followed(serve):
    other, _ = serve(lambda m, p: _json(200, {"followed": True}))
    server, transport = serve(lambda m, p: (302, {"Location": other.url + "/api/v1/x"}, b""))

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/connector/capabilities")

    assert refused.value.kind == "redirect"
    assert other.seen == []


def test_a_refusal_carries_only_the_servers_code_and_sentence(serve):
    detail = {"detail": {"error": "operation_conflict", "message": "Already submitted."}}
    _server, transport = serve(lambda m, p: _json(409, detail, **{"Retry-After": "7"}))

    with pytest.raises(HttpError) as refused:
        transport.request_json("POST", "/api/v1/connector/submissions", body={"a": 1})

    assert (refused.value.status, refused.value.code, refused.value.message) == (
        409,
        "operation_conflict",
        "Already submitted.",
    )
    assert refused.value.retry_after == 7.0


def test_an_html_error_page_is_never_shown(serve):
    _server, transport = serve(lambda m, p: (502, {"Content-Type": "text/html"}, b"<html>secret proxy page</html>"))

    with pytest.raises(HttpError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.code == "http_error" and refused.value.message == ""


def test_a_non_json_success_is_a_protocol_error(serve):
    _server, transport = serve(lambda m, p: (200, {"Content-Type": "text/html"}, b"<html>login</html>"))

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.kind == "protocol"


def test_an_oversized_answer_is_refused(serve):
    big = b'{"x":"' + b"a" * (MAX_JSON_RESPONSE + 10) + b'"}'
    _server, transport = serve(lambda m, p: (200, {"Content-Type": "application/json"}, big))

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.kind == "too_large"


def test_an_upload_streams_the_file_as_the_raw_body(serve, tmp_path):
    server, transport = serve(lambda m, p: _json(200, {"id": 7, "outcome": "created"}))
    payload = b"PK\x03\x04" + b"x" * 700_000
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(payload)
    progress = []

    status, body = transport.upload_file(
        "/api/v1/connector/files",
        str(source),
        len(payload),
        query={"filename": "part.gcode.3mf"},
        progress=lambda sent, total: progress.append(sent),
    )

    assert (status, body["id"]) == (200, 7)
    seen = server.seen[0]
    assert seen["body"] == payload
    assert seen["headers"]["Content-Type"] == "application/octet-stream"
    assert seen["path"] == "/base/api/v1/connector/files?filename=part.gcode.3mf"
    assert progress[-1] == len(payload)


def test_a_cancelled_upload_stops_between_chunks(serve, tmp_path):
    _server, transport = serve(lambda m, p: _json(200, {}))
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(b"x" * 2_000_000)

    with pytest.raises(TransportError) as refused:
        transport.upload_file("/api/v1/connector/files", str(source), 2_000_000, cancelled=lambda: True)

    assert refused.value.kind == "cancelled"


def test_nothing_outside_the_api_is_requested(serve):
    _server, transport = serve(lambda m, p: _json(200, {}))

    with pytest.raises(ValueError):
        transport.request_json("GET", "https://elsewhere.example/api/v1/x")


def test_an_unreachable_server_is_a_connect_error():
    transport = Transport(Connection.build("http://127.0.0.1:9", "bd_k"), timeout=2)

    with pytest.raises(TransportError) as refused:
        transport.request_json("GET", "/api/v1/printers/")

    assert refused.value.kind in ("connect", "timeout")


def test_tls_always_verifies():
    import ssl

    context = _ssl_context(None)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
