"""GET /system/database over HTTP."""

import pytest

from backend.app.core import query_timing as qt


@pytest.fixture(autouse=True)
def _clean_instrumentation():
    qt.reset()
    qt.set_query_threshold_ms(0)
    yield
    qt.reset()
    qt.set_query_threshold_ms(0)


@pytest.mark.asyncio
async def test_the_endpoint_answers_the_panel_shape(async_client):
    response = await async_client.get("/api/v1/system/database")
    assert response.status_code == 200
    body = response.json()

    for key in ("engine", "version", "mode", "size_bytes", "pool", "instrumentation", "probes_failed"):
        assert key in body
    assert body["mode"] in ("sqlite", "embedded", "embedded_service", "external")
    assert body["pool"]["dialect"] in ("sqlite", "postgresql")


@pytest.mark.asyncio
async def test_it_says_when_instrumentation_is_off(async_client):
    body = (await async_client.get("/api/v1/system/database")).json()
    assert body["instrumentation"]["query_threshold_ms"] == 0
    assert body["instrumentation"]["slowest"] == []
    assert body["instrumentation"]["reason"]


@pytest.mark.asyncio
async def test_it_renders_the_in_process_table_once_it_is_on(async_client):
    qt.set_query_threshold_ms(100)
    qt.record(qt.fingerprint("SELECT 1 FROM print_archives"), 250.0)
    body = (await async_client.get("/api/v1/system/database")).json()
    slowest = body["instrumentation"]["slowest"]
    assert slowest and slowest[0]["statement"] == "SELECT 1 FROM print_archives"
    assert body["instrumentation"]["reason"] is None


@pytest.mark.asyncio
async def test_it_needs_a_permission(async_client):
    response = await async_client.get("/api/v1/system/database", headers={"Authorization": "Bearer nope"})
    assert response.status_code in (401, 403)
