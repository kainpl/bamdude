"""Integration tests for the trace_id_middleware (audit B.12).

The plumbing is unit-tested in test_trace_id.py; this file exercises the
end-to-end path through Starlette: an HTTP request must always come back
with an ``X-Trace-Id`` response header, an inbound header must be
echoed when valid and replaced when malformed, and the ContextVar must
be visible to inner code (route handler) for the duration of the
request scope.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app that mirrors main.py's middleware
    decorator order: trace_id_middleware is registered LAST so it runs
    OUTERMOST, matching production behaviour."""
    from backend.app.core.trace import (
        generate_trace_id,
        get_trace_id,
        normalise_inbound_trace_id,
        trace_id_var,
    )

    app = FastAPI()

    @app.get("/echo")
    def echo():
        # Read through the public accessor so the test fails if a future
        # refactor accidentally swaps the ContextVar out from under it.
        return {"trace_id": get_trace_id()}

    @app.middleware("http")
    async def _trace_mw(request, call_next):
        inbound = normalise_inbound_trace_id(request.headers.get("X-Trace-Id"))
        trace_id = inbound if inbound is not None else generate_trace_id()
        token = trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(token)
        response.headers["X-Trace-Id"] = trace_id
        return response

    return app


def test_response_always_carries_x_trace_id_header():
    client = TestClient(_build_app())
    r = client.get("/echo")
    assert r.status_code == 200
    assert "X-Trace-Id" in r.headers
    assert re.fullmatch(r"[0-9a-f]{8}", r.headers["X-Trace-Id"])


def test_handler_sees_same_id_as_response_header():
    """The route handler reads from the ContextVar; the middleware writes
    the same value into the response header. They must agree, otherwise
    a client tracing a server-side correlation would point at a
    different log scope than the request actually ran under."""
    client = TestClient(_build_app())
    r = client.get("/echo")
    assert r.json()["trace_id"] == r.headers["X-Trace-Id"]


def test_valid_inbound_header_is_echoed():
    client = TestClient(_build_app())
    r = client.get("/echo", headers={"X-Trace-Id": "client-corr-12345"})
    assert r.headers["X-Trace-Id"] == "client-corr-12345"
    assert r.json()["trace_id"] == "client-corr-12345"


def test_malformed_inbound_header_is_replaced_not_rejected():
    """Reject the *value*, not the request — falling through to a freshly
    minted ID keeps the endpoint usable while denying log-injection
    payloads any path into the formatter."""
    client = TestClient(_build_app())
    r = client.get("/echo", headers={"X-Trace-Id": "bad value with space"})
    assert r.status_code == 200
    # Whatever came back, it can't be the malformed input.
    assert r.headers["X-Trace-Id"] != "bad value with space"
    assert re.fullmatch(r"[0-9a-f]{8}", r.headers["X-Trace-Id"])


def test_oversized_inbound_header_is_replaced():
    client = TestClient(_build_app())
    huge = "a" * 1000
    r = client.get("/echo", headers={"X-Trace-Id": huge})
    assert r.headers["X-Trace-Id"] != huge
    assert re.fullmatch(r"[0-9a-f]{8}", r.headers["X-Trace-Id"])


def test_consecutive_requests_get_distinct_ids():
    """Mostly a sanity check on the generator path — but also guards
    against a regression where the ContextVar's reset() is omitted and
    the previous request's ID leaks into the next."""
    client = TestClient(_build_app())
    ids = {client.get("/echo").headers["X-Trace-Id"] for _ in range(10)}
    # Allow for an astronomically unlikely 32-bit collision but flag the
    # all-same-id failure mode.
    assert len(ids) >= 9
