"""The production-image smoke must reject HTTP errors and SPA fallbacks, without printing credentials."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.ci import docker_http_smoke as smoke


@pytest.fixture
def server(monkeypatch):
    responses = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, content_type, body = responses[self.path]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(smoke, "BASE_URL", f"http://127.0.0.1:{http.server_port}")
    try:
        yield responses
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_http_error_is_not_success_or_a_secret_in_output(server):
    server["/api"] = (500, "application/json", b'{"access_token":"do-not-print-me"}')
    with pytest.raises(RuntimeError, match="HTTP 500, expected 200") as error:
        smoke.request("/api")
    assert "do-not-print-me" not in str(error.value)


def test_an_expected_auth_refusal_is_checked(server):
    server["/api"] = (401, "application/json", b'{"detail":"unauthorized"}')
    assert smoke.request("/api", status=401) == {"detail": "unauthorized"}
    server["/api"] = (200, "application/json", b"{}")
    with pytest.raises(RuntimeError, match="HTTP 200, expected 401"):
        smoke.request("/api", status=401)


def test_api_html_is_not_a_successful_json_response(server):
    server["/api"] = (200, "text/html", b"<html>SPA</html>")
    with pytest.raises(RuntimeError, match="expected JSON"):
        smoke.request("/api")


def test_missing_asset_returning_spa_html_is_not_success(server):
    server["/"] = (200, "text/html", b'<script src="/assets/app.js"></script>')
    server["/assets/app.js"] = (200, "text/html", b"<html>SPA</html>")
    with pytest.raises(AssertionError, match="SPA fallback"):
        smoke.check_static()
    server["/assets/app.js"] = (200, "text/javascript", b"console.log('ok');")
    smoke.check_static()
