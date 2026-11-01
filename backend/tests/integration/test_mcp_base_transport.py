"""Real SDK/socket coverage of the opt-in transport and request authority."""

import asyncio
import hashlib
import io
import json
import socket
import zipfile
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from backend.app.mcp_server.config import McpConfig
from backend.app.mcp_server.packages import SKILL_ROOT
from backend.app.mcp_server.runtime import mcp_runtime


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
            assert {tool.name for tool in listing.tools} == {"bamdude_get_capabilities", "bamdude_get_usage_guide"}
            result = await client.call_tool("bamdude_get_capabilities", {})
            data = result.structured_content["data"]
            assert data["enabled_modules"] == ["base"]
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


async def test_disable_enable_uses_a_new_manager(async_client, runtime):
    first = runtime._manager_task
    assert await runtime.configure(McpConfig())
    assert first.done()
    assert await runtime.configure(McpConfig(enabled=True))
    assert runtime._manager_task is not first


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
    finally:
        release.set()
    assert await restore
    assert not runtime._auth_tasks
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not await runtime.configure(McpConfig(enabled=True))
    await runtime.reload(after_restore=True)
    assert await runtime.configure(McpConfig(enabled=True))
