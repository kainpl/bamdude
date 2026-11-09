"""Exercise MCP policy saves on separate connections through the settings API."""

import asyncio
from contextvars import ContextVar

import pytest
from sqlalchemy import event, select

from backend.app.api.routes import settings as route
from backend.app.models.settings import Settings
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

SETTINGS_URL = "/api/v1/settings/"
HTTP_URL = "http://other.lan/mcp"
HTTPS_URL = "https://farm.lan/mcp"
ACKNOWLEDGEMENTS = {"mcp_http_risk_acknowledged": True, "mcp_http_trusted_network_acknowledged": True}
ENABLE = {
    "mcp_allow_http": True,
    **ACKNOWLEDGEMENTS,
    "mcp_endpoint_url": "http://farm.lan/mcp",
}
REVOKE = {"mcp_allow_http": False, "mcp_endpoint_url": HTTPS_URL}


async def _overlap_policy_writes(test_engine, monkeypatch, first, second, *, initial_policy):
    first_read = asyncio.Event()
    release_first = asyncio.Event()
    second_lock_attempt = asyncio.Event()
    is_second = ContextVar("is_second", default=False)
    first_connection = second_connection = None
    read_policy = route.get_setting

    async def pause_first_after_read(db, key):
        nonlocal first_connection
        value = await read_policy(db, key)
        if key == "mcp_allow_http" and not is_second.get() and not first_read.is_set():
            assert value == initial_policy
            first_connection = (await db.connection()).sync_connection.connection.driver_connection
            first_read.set()
            await release_first.wait()
        return value

    def watch_sql(conn, cursor, statement, parameters, context, executemany):
        nonlocal second_connection
        if is_second.get() and (statement == "BEGIN IMMEDIATE" or statement.startswith("SELECT pg_advisory_xact_lock")):
            second_connection = conn.connection.driver_connection
            second_lock_attempt.set()

    async def run_second():
        token = is_second.set(True)
        try:
            return await second()
        finally:
            is_second.reset(token)

    monkeypatch.setattr(route, "get_setting", pause_first_after_read)
    event.listen(test_engine.sync_engine, "before_cursor_execute", watch_sql)
    try:
        async with asyncio.timeout(10):
            async with asyncio.TaskGroup() as tasks:
                first_request = tasks.create_task(first())
                await first_read.wait()
                second_request = tasks.create_task(run_second())
                attempted = tasks.create_task(second_lock_attempt.wait())
                try:
                    # The old route completes the second request while the first
                    # is still paused. A serialized writer instead reaches the
                    # database lock, so release the owner before waiting for it.
                    await asyncio.wait({second_request, attempted}, return_when=asyncio.FIRST_COMPLETED)
                    waited_for_lock = second_lock_attempt.is_set() and not second_request.done()
                finally:
                    attempted.cancel()
                    release_first.set()
        assert first_connection is not second_connection
        return first_request.result(), second_request.result(), waited_for_lock
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", watch_sql)


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize("explicit_permission", [False, True])
@pytest.mark.parametrize("revoke_first", [False, True])
async def test_http_settings_save_serializes_with_revocation(
    async_client, test_engine, monkeypatch, method, explicit_permission, revoke_first
):
    enabled = await async_client.request(method, SETTINGS_URL, json=ENABLE)
    assert enabled.status_code == 200, enabled.text
    update = {"mcp_endpoint_url": HTTP_URL}
    if explicit_permission:
        update["mcp_allow_http"] = True

    async def save():
        return await async_client.request(method, SETTINGS_URL, json=update)

    async def revoke():
        return await async_client.request(method, SETTINGS_URL, json=REVOKE)

    first, second = (revoke, save) if revoke_first else (save, revoke)
    first_result, second_result, waited_for_lock = await _overlap_policy_writes(
        test_engine, monkeypatch, first, second, initial_policy="true"
    )

    state = (await async_client.get(SETTINGS_URL)).json()
    assert (state["mcp_allow_http"], state["mcp_endpoint_url"]) == (False, HTTPS_URL)
    assert first_result.status_code == 200, first_result.text
    assert second_result.status_code == (422 if revoke_first else 200), second_result.text
    assert waited_for_lock


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
@pytest.mark.parametrize("reset", [False, True])
async def test_mcp_http_first_save_serializes_without_policy_row(async_client, test_engine, monkeypatch, method, reset):
    async def enable():
        return await async_client.request(method, SETTINGS_URL, json=ENABLE)

    async def follow_up():
        if reset:
            return await async_client.post(SETTINGS_URL + "reset")
        return await async_client.request(method, SETTINGS_URL, json={"mcp_endpoint_url": HTTP_URL})

    first, second, waited_for_lock = await _overlap_policy_writes(
        test_engine, monkeypatch, enable, follow_up, initial_policy=None
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    state = (await async_client.get(SETTINGS_URL)).json()
    expected = (False, "") if reset else (True, HTTP_URL)
    assert (state["mcp_allow_http"], state["mcp_endpoint_url"]) == expected
    assert ACKNOWLEDGEMENTS.keys().isdisjoint(state)
    async with test_engine.connect() as conn:
        keys = (await conn.scalars(select(Settings.key))).all()
    assert ACKNOWLEDGEMENTS.keys().isdisjoint(keys)
    assert waited_for_lock
