"""BA13 verifies original LogRecords, independent of known-value redaction."""

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.app.services import rest_smart_plug


class RawRecords(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.msg, record.args, record.getMessage(), record.exc_info))


@pytest.fixture
def records():
    logger = rest_smart_plug.logger
    handler = RawRecords()
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["turn_on", "turn_off"])
@pytest.mark.parametrize("failure", [None, "timeout", "http", "connect", "unexpected"])
async def test_runtime_never_logs_capabilities(monkeypatch, records, operation, failure):
    url = "https://USER_CANARY:PASS_CANARY@bridge.example:8443/PATH_CANARY?secret=QUERY_CANARY#FRAGMENT_CANARY"
    headers = '{"Authorization":"HEADER_CANARY","X-Capability":"HEADER_CANARY"}'
    body = "BODY_CANARY"
    seen = []

    def peer(request):
        seen.append(request)
        # Driver messages can contain request URL, headers or even payloads.
        message = url + " " + headers + " " + body
        if failure == "timeout":
            raise httpx.ReadTimeout(message, request=request)
        if failure == "connect":
            raise httpx.ConnectError(message, request=request)
        if failure == "unexpected":
            raise RuntimeError(message)
        return httpx.Response(503 if failure == "http" else 200, text=message)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        rest_smart_plug.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(peer), **kw)
    )
    plug = SimpleNamespace(
        name="Workshop",
        rest_on_url=url,
        rest_off_url=url,
        rest_on_body=body,
        rest_off_body=body,
        rest_headers=headers,
        rest_method="POST",
    )
    assert await getattr(rest_smart_plug.RESTSmartPlugService(), operation)(plug) is (failure is None)
    # httpx derives Basic auth from URL userinfo; the separate capability header
    # still reaches the peer unchanged alongside it.
    assert seen and seen[0].headers["X-Capability"] == "HEADER_CANARY"
    assert seen[0].content == b"BODY_CANARY"
    # These are the original message template/args, before any app handler's
    # formatter/filter or diagnostic collector could hide a leaked value.
    assert records and "CANARY" not in repr(records)
    assert any("https://bridge.example" in item[2] for item in records)
    assert all(item[3] is None for item in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/PATH_CANARY?secret=QUERY_CANARY",
        "https://[INVALID_CANARY/path",
        "invalid_CANARY",
    ],
)
async def test_rejected_url_logs_no_raw_value(records, url):
    assert await rest_smart_plug.RESTSmartPlugService()._send_request(url) is None
    assert records and "CANARY" not in repr(records)


def test_malformed_headers_are_never_logged(records):
    assert rest_smart_plug.RESTSmartPlugService()._parse_headers('{"Authorization":"HEADER_CANARY"') == {}
    assert records and "CANARY" not in repr(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "http", "unexpected"])
async def test_draft_driver_error_is_static(monkeypatch, failure):
    def peer(request):
        if failure == "connect":
            raise httpx.ConnectError("DRIVER_CANARY", request=request)
        if failure == "unexpected":
            raise RuntimeError("DRIVER_CANARY")
        return httpx.Response(503, text="BODY_CANARY", extensions={"reason_phrase": b"DRIVER_CANARY"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        rest_smart_plug.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(peer), **kw)
    )
    result = await rest_smart_plug.RESTSmartPlugService().test_connection("https://bridge.example/PATH_CANARY")
    expected = {"connect": "Connection failed", "http": "HTTP 503", "unexpected": "REST connection failed"}
    assert result == {"success": False, "error": expected[failure]}


def test_debug_app_suppresses_http_wire_capabilities(tmp_path):
    """Use real app logger setup, without lifespan, DB access or real network."""
    script = textwrap.dedent("""\
        import asyncio
        import logging
        from types import SimpleNamespace
        import httpx
        from backend.app.main import app
        from backend.app.core.config import settings
        from backend.app.services.rest_smart_plug import RESTSmartPlugService

        assert settings.debug is True
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore.connection").getEffectiveLevel() == logging.WARNING
        seen = []
        def peer(request):
            seen.append(request)
            assert request.headers["X-Capability"] == "HEADER_CANARY"
            return httpx.Response(200)
        client = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: client(transport=httpx.MockTransport(peer), **kw)
        plug = SimpleNamespace(
            name="Workshop", rest_on_url="https://bridge.example/PATH_CANARY?token=QUERY_CANARY",
            rest_headers='{"X-Capability":"HEADER_CANARY"}', rest_method="POST", rest_on_body=None,
        )
        assert asyncio.run(RESTSmartPlugService().turn_on(plug))
        assert len(seen) == 1
        # MockTransport emits the real httpx INFO request log; httpcore's wire
        # logger is checked separately without opening a socket.
        logging.getLogger("httpcore.connection").debug("wire headers=%s", "HEADER_CANARY")
        print("DEBUG_REST_LOGGING_OK")
    """)
    env = {
        **os.environ,
        "DEBUG": "true",
        "LOG_TO_FILE": "false",
        "BAMDUDE_IGNORE_DOTENV": "1",
        "DATA_DIR": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[4],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "DEBUG_REST_LOGGING_OK" in output
    assert "https://bridge.example" in output
    assert "CANARY" not in output
