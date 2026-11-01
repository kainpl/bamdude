"""Bounded failure paths through the production ASGI stack; never a live farm."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app.mcp_server.config import McpConfig
from backend.app.mcp_server.runtime import mcp_runtime
from backend.tests.integration.test_mcp_base_transport import key, live_app

CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bamdude_get_capabilities"}}


@pytest.fixture
async def runtime():
    assert await mcp_runtime.configure(McpConfig(enabled=True))
    yield mcp_runtime
    await mcp_runtime.shutdown()


async def controls(client):
    assert (await client.get("/health")).json() == {"status": "healthy"}
    assert (await client.get("/api/v1/settings/")).status_code == 200
    assert (await client.get("/api/v1/queue/")).status_code == 200


async def test_ingress_flood_is_bounded_before_auth_and_rest_stays_available(async_client, runtime, monkeypatch):
    from backend.app.mcp_server import runtime as module

    created = await key(async_client)
    entered, release = asyncio.Queue(), asyncio.Event()
    original = module.authenticate

    async def blocked(scope):
        await entered.put(None)
        await release.wait()
        return await original(scope)

    monkeypatch.setattr(module, "authenticate", blocked)
    from backend.app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        tasks = [
            asyncio.create_task(http.post("/mcp", headers={"X-API-Key": created["key"]}, json=CALL)) for _ in range(16)
        ]
        try:
            async with asyncio.timeout(5):
                for _ in tasks:
                    await entered.get()
            assert runtime._admitted == 16 and len(runtime._auth_tasks) == 16
            refused = await http.post("/mcp", headers={"X-API-Key": created["key"]}, json=CALL)
            assert refused.status_code == 503 and refused.json()["code"] == "busy"
            assert entered.empty()  # overflow never entered authentication
            await controls(async_client)
        finally:
            for task in tasks:
                task.cancel()
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*tuple(runtime._auth_tasks), return_exceptions=True)
        assert runtime._admitted == 0 and not runtime._requests and not runtime._auth_tasks


@pytest.mark.parametrize("failure", ["chunked_body", "oversized_output", "deadline", "exception"])
async def test_failed_read_is_bounded_and_releases_admission(async_client, runtime, monkeypatch, caplog, failure):
    from backend.app.mcp_server import runtime as module

    created = await key(async_client)
    entered = []
    finalized = asyncio.Event()
    secret = "CANARY_mcp_reader_exception_must_not_escape"

    async def reader(scope, receive, send):
        entered.append(True)
        try:
            if failure == "exception":
                raise RuntimeError(secret)
            if failure == "deadline":
                await asyncio.Event().wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x" * 131073})
        finally:
            finalized.set()

    monkeypatch.setattr(runtime, "_sdk_app", reader)
    if failure == "deadline":
        # Expire the SDK scope at its first suspension, without a wall-clock sleep
        # or changing asyncio.timeout globally for the application/test runner.
        monkeypatch.setattr(
            module,
            "asyncio",
            SimpleNamespace(
                **{**vars(asyncio), "timeout": lambda seconds: asyncio.timeout(0 if seconds == 10 else seconds)}
            ),
        )

    async def chunks():
        yield b" " * 16000
        yield b" " * 16769

    from backend.app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post(
            "/mcp",
            headers={"X-API-Key": created["key"]},
            **({"content": chunks()} if failure == "chunked_body" else {"json": CALL}),
        )
    expected = {
        "chunked_body": (413, "request_too_large"),
        "oversized_output": (503, "result_too_large"),
        "deadline": (503, "deadline_exceeded"),
        "exception": (503, "source_unavailable"),
    }
    status, code = expected[failure]
    assert response.status_code == status and response.json()["code"] == code
    assert len(response.content) < 1024 and secret not in response.text and secret not in caplog.text
    assert bool(entered) is (failure != "chunked_body")
    assert finalized.is_set() is (failure != "chunked_body")
    assert runtime._admitted == 0 and runtime._domain_calls == 0 and runtime._key_calls == {}
    assert not runtime._requests and not runtime._auth_tasks
    await controls(async_client)


async def test_socket_disconnect_releases_owned_reader(async_client, runtime, monkeypatch):
    created = await key(async_client)
    entered, disconnected, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_dispatch = runtime.dispatch

    async def dispatch(*args):
        try:
            await original_dispatch(*args)
        finally:
            finished.set()

    async def reader(scope, receive, send):
        assert (await receive())["type"] == "http.request"
        entered.set()
        while (await receive())["type"] != "http.disconnect":
            pass
        disconnected.set()

    monkeypatch.setattr(runtime, "_sdk_app", reader)
    monkeypatch.setattr(runtime, "dispatch", dispatch)
    async with live_app() as url:
        port = int(url.rsplit(":", 1)[1])
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps(CALL).encode()
        writer.write(
            (
                f"POST /mcp HTTP/1.1\r\nHost: localhost\r\nX-API-Key: {created['key']}\r\nContent-Length: {len(body)}\r\nContent-Type: application/json\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            assert runtime._domain_calls == 1
        finally:
            writer.close()
            await writer.wait_closed()
        async with asyncio.timeout(5):
            await disconnected.wait()
            await finished.wait()  # outer admission ends after the owned reader
        assert runtime._admitted == 0 and runtime._domain_calls == 0 and runtime._key_calls == {}
        await controls(async_client)


async def test_sdk_start_failure_keeps_host_available(async_client, monkeypatch, caplog):
    from backend.app.mcp_server import registry

    secret = "CANARY_mcp_start_exception_must_not_escape"

    def unavailable():
        raise RuntimeError(secret)

    monkeypatch.setattr(registry, "create_server", unavailable)
    try:
        assert not await mcp_runtime.configure(McpConfig(enabled=True))
        assert mcp_runtime.state == "unavailable" and mcp_runtime.reason == "runtime_unavailable"
        response = await async_client.post("/mcp", json=CALL, headers={"Host": "localhost"})
        assert response.status_code == 503 and secret not in response.text and secret not in caplog.text
        await controls(async_client)
    finally:
        await mcp_runtime.shutdown()
