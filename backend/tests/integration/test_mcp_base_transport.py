"""Real SDK/socket coverage of the opt-in transport and request authority."""

import asyncio
import hashlib
import io
import json
import socket
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from backend.app.mcp_server.config import McpConfig
from backend.app.mcp_server.packages import SKILL_ROOT
from backend.app.mcp_server.runtime import McpMiddleware, McpRuntime, mcp_runtime


@asynccontextmanager
async def live_app():
    from backend.app.main import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runner = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error"))
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not runner.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        runner.should_exit = True
        await task
        sock.close()


async def key(client, *, mcp=True, scope=None):
    response = await client.post(
        "/api/v1/api-keys/",
        json={
            "name": "test assistant",
            "can_access_mcp": mcp,
            "printer_ids": scope,
            "can_queue": False,
            "can_control_printer": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def runtime():
    assert await mcp_runtime.configure(McpConfig(enabled=True))
    yield mcp_runtime
    await mcp_runtime.shutdown()


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_sdk_base_over_real_socket(async_client, runtime, mode):
    created = await key(async_client)
    async with live_app() as url:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {created['key']}"}) as http,
            Client(streamable_http_client(url + "/mcp", http_client=http), mode=mode, cache=None) as client,
        ):
            assert client.protocol_version == ("2025-11-25" if mode == "legacy" else "2026-07-28")
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "bamdude_get_capabilities",
                "bamdude_get_usage_guide",
                "bamdude_get_farm_overview",
                "bamdude_inspect_printer",
                "bamdude_get_system_health",
                "bamdude_get_print_timeline",
                "bamdude_inspect_energy",
                "bamdude_list_state_issues",
                "bamdude_explain_recovery",
            }
            result = await client.call_tool("bamdude_get_capabilities", {})
            data = result.structured_content["data"]
            assert data["enabled_modules"] == ["base", "diagnostics"]
            assert data["auth"] == "api_key"
            guide = await client.call_tool("bamdude_get_usage_guide", {})
            assert "never in a chat" in guide.structured_content["data"]["guide"]
            direct = await client.session.call_tool("bamdude_repair_database", {})
            assert direct.is_error
            resource = await client.read_resource("bamdude://guide")
            assert resource.contents[0].text == guide.structured_content["data"]["guide"]
            prompt = await client.get_prompt("bamdude_diagnose")
            assert "base module alone" in prompt.messages[0].content.text
        assert runtime.observed_connection(created["id"])


@pytest.mark.parametrize("limit", ["key", "process", "minute"])
async def test_advertised_limits_match_runtime_admission(async_client, runtime, monkeypatch, limit):
    from backend.app.mcp_server import runtime as module
    from backend.app.services.queue_sources import current_epoch

    created = await key(async_client)
    async with (
        live_app() as url,
        httpx2.AsyncClient(headers={"X-API-Key": created["key"]}) as http,
        Client(streamable_http_client(url + "/mcp", http_client=http), cache=None) as client,
    ):
        capabilities = await client.call_tool("bamdude_get_capabilities", {})
        limits = capabilities.structured_content["data"]["limits"]
    assert limits == {
        "page_default": 50,
        "page_max": 100,
        "response_bytes": 131072,
        "concurrent_calls_per_key": 2,
        "concurrent_calls_per_process": 8,
        "calls_per_minute_per_key": 60,
    }

    # Exercise the production admission path without hashing a key sixty times.
    async def authenticated(scope):
        key_id = int(dict(scope["headers"]).get(b"x-test-key", b"1"))
        return SimpleNamespace(key_id=key_id, epoch=current_epoch())

    monkeypatch.setattr(module, "authenticate", authenticated)
    admitted = asyncio.Queue()
    release = asyncio.Event()

    async def reader(scope, receive, send):
        await admitted.put(None)
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    local = McpRuntime()
    local.config = McpConfig(enabled=True)
    local.state = "ready"
    local._sdk_app = reader
    transport = httpx.ASGITransport(app=McpMiddleware(None, runtime=local))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:

        async def call(key_id=1):
            return await http.post(
                "/mcp",
                headers={"X-Test-Key": str(key_id)},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "bamdude_get_capabilities"},
                },
            )

        if limit == "minute":
            release.set()
            for _ in range(limits["calls_per_minute_per_key"]):
                assert (await call()).status_code == 200
            refusal = await call()
            assert refusal.status_code == 429 and refusal.json()["code"] == "rate_limited"
            # Expiring the rolling window makes room; this is not a lifetime quota.
            expired = module.time.monotonic() - 61
            local._rate[1] = type(local._rate[1])([expired] * limits["calls_per_minute_per_key"])
            assert (await call()).status_code == 200
        else:
            count = limits[f"concurrent_calls_per_{limit}"]
            key_ids = (
                [1] * count if limit == "key" else [1 + i // limits["concurrent_calls_per_key"] for i in range(count)]
            )
            tasks = [asyncio.create_task(call(key_id)) for key_id in key_ids]
            try:
                async with asyncio.timeout(5):
                    for _ in tasks:
                        await admitted.get()
                assert local._domain_calls == count
                refusal = await call(1 if limit == "key" else 999)
                assert refusal.status_code == 503 and refusal.json()["code"] == "busy"
            finally:
                release.set()
                responses = await asyncio.gather(*tasks)
            assert all(response.status_code == 200 for response in responses)
            assert local._domain_calls == 0 and local._key_calls == {}
            assert (await call()).status_code == 200


async def test_mcp_is_disabled_without_affecting_rest(async_client):
    await mcp_runtime.shutdown()
    response = await async_client.post("/mcp", json={})
    assert response.status_code == 404
    assert (await async_client.get("/api/v1/settings/")).status_code == 200


async def test_paths_credentials_hosts_and_origins(async_client, runtime):
    allowed = await key(async_client)
    plain = await key(async_client, mcp=False)
    for path in ("/mcp/",):
        response = await async_client.post(path, json={})
        assert response.status_code == 404 and "location" not in response.headers
    async with live_app() as url, httpx.AsyncClient(base_url=url, follow_redirects=False) as http:
        for headers, status in [
            ({}, 401),
            ({"Authorization": async_client.headers["Authorization"]}, 401),
            ({"X-API-Key": plain["key"]}, 403),
            ({"X-API-Key": allowed["key"], "Authorization": "Bearer bd_different"}, 401),
            ({"X-API-Key": allowed["key"], "Host": "attacker.invalid"}, 403),
            ({"X-API-Key": allowed["key"], "Origin": "null"}, 403),
        ]:
            response = await http.post("/mcp", headers=headers, json={})
            assert response.status_code == status, response.text
            assert response.headers["cache-control"] == "no-store"
            assert "www-authenticate" not in response.headers
        oversized = await http.post("/mcp", headers={"X-API-Key": allowed["key"]}, content=b" " * 32769)
        assert oversized.status_code == 413


async def test_rest_does_not_mark_mcp_connection_and_restart_clears_marker(async_client, runtime):
    created = await key(async_client)
    response = await async_client.get("/api/v1/settings/", headers={"X-API-Key": created["key"]})
    assert response.status_code == 200
    assert runtime.observed_connection(created["id"]) is None
    async with live_app() as url, httpx.AsyncClient() as http:
        await http.post(url + "/mcp", headers={"X-API-Key": created["key"]}, json={})
    assert runtime.observed_connection(created["id"])
    await runtime.shutdown()
    assert runtime.observed_connection(created["id"]) is None
    assert await runtime.configure(McpConfig(enabled=True))


async def test_disable_enable_uses_a_new_manager(async_client, monkeypatch):
    from backend.app.mcp_server import registry

    servers = []
    original = registry.create_server

    def create_server():
        server = original()
        servers.append(server)
        return server

    monkeypatch.setattr(registry, "create_server", create_server)
    try:
        assert await mcp_runtime.configure(McpConfig(enabled=True))
        first = mcp_runtime._manager_task
        assert await mcp_runtime.configure(McpConfig())
        assert first.done()
        assert await mcp_runtime.configure(McpConfig(enabled=True))
        assert mcp_runtime._manager_task is not first
        assert len(servers) == 2
        assert servers[1] is not servers[0]
    finally:
        await mcp_runtime.shutdown()


async def test_modern_discovery_requires_a_credential(async_client, runtime):
    discover = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": "server/discover",
    }
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        refused = await http.post(url + "/mcp", headers=headers, json=discover)
        assert refused.status_code == 401, refused.text
        allowed = await http.post(url + "/mcp", headers={**headers, "X-API-Key": created["key"]}, json=discover)
        assert allowed.status_code == 200, allowed.text
        assert "result" in allowed.json()


async def test_result_of_an_older_epoch_is_not_published(async_client, runtime, monkeypatch):
    from backend.app.services import queue_sources

    created = await key(async_client)
    original = runtime._sdk_app
    collected = []

    async def restore_after_read(scope, receive, send):
        async def collect(message):
            collected.append(message)
            await send(message)

        await original(scope, receive, collect)
        # The actual SDK has finished, but the transport has not published its
        # buffered result. Invalidate the same epoch used by database restore.
        queue_sources.invalidate_receipts()

    monkeypatch.setattr(runtime, "_sdk_app", restore_after_read)
    async with live_app() as url, httpx.AsyncClient() as http:
        response = await http.post(
            url + "/mcp",
            headers={"Accept": "application/json, text/event-stream", "X-API-Key": created["key"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bamdude_get_capabilities"}},
        )
    assert collected[0]["status"] == 200
    body = b"".join(message.get("body", b"") for message in collected)
    assert "result" in json.loads(body)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "state_changed"
    assert "result" not in response.json()


async def test_mcp_key_loses_access_when_read_status_is_removed(async_client, runtime):
    created = await key(async_client)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bamdude_get_capabilities"}}
    headers = {"Accept": "application/json, text/event-stream", "X-API-Key": created["key"]}
    async with live_app() as url, httpx.AsyncClient() as http:
        allowed = await http.post(url + "/mcp", headers=headers, json=call)
        assert allowed.status_code == 200, allowed.text
        changed = await async_client.patch(f"/api/v1/api-keys/{created['id']}", json={"can_read_status": False})
        assert changed.status_code == 200, changed.text
        assert changed.json()["can_access_mcp"] is True
        refused = await http.post(url + "/mcp", headers=headers, json=call)
        assert refused.status_code == 403, refused.text


@pytest.mark.parametrize("origin, status", [("https://assistant.example", 204), ("https://attacker.invalid", 403)])
async def test_preflight_checks_origin_without_a_key(async_client, runtime, origin, status):
    assert await runtime.configure(McpConfig(enabled=True, allowed_origins=["https://assistant.example"]))
    async with live_app() as url, httpx.AsyncClient() as http:
        response = await http.options(
            url + "/mcp",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-API-Key, MCP-Protocol-Version",
            },
        )
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    if status == 204:
        assert response.headers["access-control-allow-origin"] == origin
        assert "POST" in response.headers["access-control-allow-methods"]
        assert {"x-api-key", "mcp-protocol-version"} <= {
            value.strip().lower() for value in response.headers["access-control-allow-headers"].split(",")
        }
    else:
        assert response.json()["code"] == "origin_denied"
        assert "access-control-allow-origin" not in response.headers


async def test_forwarded_host_does_not_override_host_allowlist(async_client, runtime):
    assert await runtime.configure(McpConfig(enabled=True, allowed_hosts=["farm.example:8443"]))
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        response = await http.post(
            url + "/mcp",
            headers={
                "X-API-Key": created["key"],
                "Host": "attacker.invalid",
                "X-Forwarded-Host": "farm.example:8443",
            },
            json={},
        )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "host_denied"


async def test_revoke_applies_to_the_next_http_request(async_client, runtime):
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        headers = {"Authorization": f"Bearer {created['key']}"}
        await http.post(url + "/mcp", headers=headers, json={})
        deleted = await async_client.delete(f"/api/v1/api-keys/{created['id']}")
        assert deleted.status_code == 200
        response = await http.post(url + "/mcp", headers=headers, json={})
        assert response.status_code == 401


async def test_settings_validate_persist_and_reconfigure(async_client):
    await mcp_runtime.shutdown()
    try:
        for invalid in [
            {"mcp_allowed_hosts": ["*"]},
            {"mcp_allowed_hosts": ["trusted.example/path"]},
            {"mcp_allowed_origins": ["null"]},
            {"mcp_allowed_origins": ["https://trusted.example/path"]},
            {"mcp_allowed_origins": None},
        ]:
            response = await async_client.patch("/api/v1/settings/", json=invalid)
            assert response.status_code == 422, response.text
        updated = await async_client.patch(
            "/api/v1/settings/",
            json={
                "mcp_enabled": True,
                "mcp_allowed_hosts": ["farm.example:8443"],
                "mcp_allowed_origins": ["https://assistant.example"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["mcp_enabled"] is True
        assert mcp_runtime.state == "ready"
        readback = await async_client.get("/api/v1/settings/")
        assert readback.json()["mcp_allowed_hosts"] == ["farm.example:8443"]
        assert readback.json()["mcp_allowed_origins"] == ["https://assistant.example"]
    finally:
        await mcp_runtime.shutdown()


async def test_skill_download_matches_guide_and_requires_permission(async_client):
    first = await async_client.get("/api/v1/integrations/mcp/skill.zip")
    second = await async_client.get("/api/v1/integrations/mcp/skill.zip")
    assert first.status_code == 200, first.text
    assert first.content == second.content
    assert first.headers["x-content-sha256"] == hashlib.sha256(first.content).hexdigest()
    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
        assert set(archive.namelist()) == {
            "bamdude-diagnostics/SKILL.md",
            "bamdude-diagnostics/references/diagnostics.md",
            "bamdude-diagnostics/manifest.json",
        }
        manifest = json.loads(archive.read("bamdude-diagnostics/manifest.json"))
        for name, digest in manifest["sha256"].items():
            assert hashlib.sha256(archive.read("bamdude-diagnostics/" + name)).hexdigest() == digest
        assert (
            archive.read("bamdude-diagnostics/references/diagnostics.md")
            == (SKILL_ROOT / "references/diagnostics.md").read_bytes()
        )
    response = await async_client.get("/api/v1/integrations/mcp/skill.zip", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401


async def test_connection_marker_is_not_exposed_to_a_read_status_key(async_client, runtime):
    created = await key(async_client)
    response = await async_client.get(
        f"/api/v1/integrations/mcp/keys/{created['id']}/connection",
        headers={"Authorization": f"Bearer {created['key']}"},
    )
    assert response.status_code == 403
    allowed = await async_client.get(f"/api/v1/integrations/mcp/keys/{created['id']}/connection")
    assert allowed.status_code == 200
    assert allowed.json() == {"observed_at": None}


async def test_guide_failure_does_not_publish_secret_exception(async_client, runtime, monkeypatch, caplog):
    from backend.app.mcp_server import registry

    secret = "CANARY_secret_not_for_the_model"

    def failed_guide():
        raise RuntimeError(secret)

    monkeypatch.setattr(registry, "usage_guide", failed_guide)
    created = await key(async_client)
    async with (
        live_app() as url,
        httpx2.AsyncClient(headers={"X-API-Key": created["key"]}) as http,
        Client(streamable_http_client(url + "/mcp", http_client=http), cache=None) as client,
    ):
        response = await client.call_tool("bamdude_get_usage_guide", {})
        assert response.is_error
        assert "source_unavailable" in str(response)
        assert secret not in str(response)
    assert secret not in caplog.text


async def test_restore_waits_for_owned_auth_after_request_cancellation(async_client, runtime, monkeypatch):
    from backend.app.mcp_server import runtime as module

    entered, release = asyncio.Event(), asyncio.Event()
    original = module.authenticate

    async def delayed(scope):
        entered.set()
        await release.wait()
        return await original(scope)

    monkeypatch.setattr(module, "authenticate", delayed)
    created = await key(async_client)
    request = asyncio.create_task(
        async_client.post("/mcp", json={}, headers={"Host": "localhost", "X-API-Key": created["key"]})
    )
    await entered.wait()
    restore = asyncio.create_task(runtime.quiesce_for_restore())
    try:
        await asyncio.sleep(0)  # run the quiesce cancellation, not a time-based lock proof
        assert not restore.done()
        assert runtime._auth_tasks
        status = await async_client.get("/api/v1/integrations/mcp/status")
        assert status.json()["state"] == "stopping"
        assert status.json()["reason"] == "restore_in_progress"
    finally:
        release.set()
    assert await restore
    status = await async_client.get("/api/v1/integrations/mcp/status")
    assert status.json()["state"] == "unavailable"
    assert status.json()["reason"] == "restore_hold"
    assert not runtime._auth_tasks
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not await runtime.configure(McpConfig(enabled=True))
    await runtime.reload(after_restore=True)
    assert await runtime.configure(McpConfig(enabled=True))


async def test_refused_restore_status_is_unavailable_until_reinitialized(async_client, runtime, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_drain", AsyncMock(return_value=False))
        assert not await runtime.quiesce_for_restore()
    status = await async_client.get("/api/v1/integrations/mcp/status")
    assert status.json()["desired_enabled"] is True
    assert status.json()["state"] == "unavailable"
    assert status.json()["reason"] == "readers_still_running"
    assert not await runtime.reload()
    assert not await runtime.configure(McpConfig(enabled=True))
    response = await async_client.post("/mcp", json={}, headers={"Host": "localhost"})
    assert response.status_code == 503
    await runtime.reload(after_restore=True)
    assert await runtime.configure(McpConfig(enabled=True))
