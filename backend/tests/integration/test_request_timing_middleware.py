"""The outermost middleware.

It must time auth's early returns too, its Server-Timing header must carry both
numbers, and the database share must be attributed to the request that issued
the statements — the split that tells «the event loop is saturated» apart from
«this endpoint runs 41 queries».
"""

import pytest

from backend.app.core import query_timing as qt


@pytest.fixture(autouse=True)
def _thresholds_off():
    qt.set_request_threshold_ms(0)
    qt.set_query_threshold_ms(0)
    qt.reset()
    yield
    qt.set_request_threshold_ms(0)
    qt.set_query_threshold_ms(0)
    qt.reset()


@pytest.mark.asyncio
async def test_every_response_carries_server_timing(async_client):
    response = await async_client.get("/health")
    assert response.status_code == 200
    header = response.headers["server-timing"]
    assert "db;dur=" in header
    assert "total;dur=" in header


@pytest.mark.asyncio
async def test_an_unauthenticated_api_call_is_still_timed(async_client):
    """auth_middleware returns before any route runs; outermost is the only
    position that sees it at all."""
    response = await async_client.get("/api/v1/printers/", headers={"Authorization": "Bearer nope"})
    assert response.status_code in (401, 403, 503)
    assert "server-timing" in response.headers


@pytest.mark.asyncio
async def test_a_slow_request_is_logged_with_its_database_split(async_client, caplog):
    qt.set_request_threshold_ms(1)
    with caplog.at_level("WARNING", logger="backend.app.core.query_timing"):
        await async_client.get("/health")
    line = next(r.getMessage() for r in caplog.records if "slow request" in r.getMessage())
    assert "GET /health" in line
    # /health touches no database at all — that zero IS the diagnosis.
    assert "db 0 queries" in line


@pytest.mark.asyncio
async def test_a_request_that_touches_the_database_reports_its_queries(async_client, test_engine, caplog):
    """The load-bearing claim of the whole design.

    ⚠️ ``install`` here is not a workaround — conftest's ``test_engine`` attaches
    none of ``database.py``'s listeners, so without this the count would be 0 for
    a request that ran a dozen statements and the test would pass while proving
    nothing.

    What it actually proves: the accumulator set in the OUTER middleware task is
    the same object the listener mutates inside the child task Starlette spawns
    for the inner app. A plain float in the ContextVar would report 0 here.
    """
    qt.install(test_engine)
    qt.set_request_threshold_ms(1)
    with caplog.at_level("WARNING", logger="backend.app.core.query_timing"):
        await async_client.get("/api/v1/printers/")
    line = next(r.getMessage() for r in caplog.records if "slow request" in r.getMessage())
    assert "/api/v1/printers/" in line
    assert "db 0 queries" not in line


@pytest.mark.asyncio
async def test_the_query_string_never_reaches_the_log(async_client, caplog):
    """A query string carries filter values and, on the camera routes, a stream
    token."""
    qt.set_request_threshold_ms(1)
    with caplog.at_level("WARNING", logger="backend.app.core.query_timing"):
        await async_client.get("/api/v1/printers/?search=secret-value")
    line = next(r.getMessage() for r in caplog.records if "slow request" in r.getMessage())
    assert "secret-value" not in line


@pytest.mark.asyncio
async def test_nothing_is_logged_while_the_threshold_is_zero(async_client, caplog):
    with caplog.at_level("WARNING", logger="backend.app.core.query_timing"):
        await async_client.get("/health")
    assert not [r for r in caplog.records if "slow request" in r.getMessage()]
