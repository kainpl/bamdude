"""Complete draft pairs, write-only reads, and short transactions at real HTTP doors."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from backend.app.core.auth import create_access_token
from backend.app.models.git_backup import GitBackupConfig
from backend.app.models.group import Group
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.user import User
from backend.app.services.git_backup import git_backup_service
from backend.tests.auth_helpers import mint_test_access

pytestmark = pytest.mark.asyncio


@pytest.fixture
def git_network(monkeypatch):
    network = AsyncMock(return_value={"success": True, "message": "Connected", "is_private": True})
    monkeypatch.setattr(git_backup_service, "test_connection", network)
    return network


async def git_config(client):
    response = await client.post(
        "/api/v1/git-backup/config",
        json={
            "repository_url": "https://github.com/owner/private",
            "access_token": "CANARY_GIT",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"repository_url": "https://github.com/owner/other"},
        {"provider": "gitea", "repository_url": "https://git.example/owner/repo"},
        {"api_base_url": "https://different.example/api"},
    ],
)
async def test_git_destination_cannot_retain_token(async_client, git_network, changes):
    await git_config(async_client)
    git_network.reset_mock()
    response = await async_client.patch("/api/v1/git-backup/config", json=changes)
    assert response.status_code == 400, response.text
    git_network.assert_not_awaited()
    assert "CANARY" not in response.text
    response = await async_client.get("/api/v1/git-backup/config")
    assert response.json()["repository_url"] == "https://github.com/owner/private"


async def test_git_draft_and_clear_are_atomic(async_client, git_network, db_session):
    await git_config(async_client)
    git_network.reset_mock()
    response = await async_client.post(
        "/api/v1/git-backup/test",
        json={
            "repo_url": "https://github.com/owner/other",
            "secret_actions": {"token": "keep"},
        },
    )
    assert response.status_code == 400
    git_network.assert_not_awaited()
    response = await async_client.post(
        "/api/v1/git-backup/test",
        json={
            "repo_url": "https://github.com/owner/other",
            "token": "*literal-token*",
            "secret_actions": {"token": "replace"},
        },
    )
    assert response.status_code == 200, response.text
    assert git_network.await_args.args == ("https://github.com/owner/other", "*literal-token*")
    stored = await db_session.scalar(select(GitBackupConfig))
    assert stored.repository_url.endswith("/private") and stored.access_token == "CANARY_GIT"
    response = await async_client.patch("/api/v1/git-backup/config", json={"secret_actions": {"access_token": "clear"}})
    assert response.status_code == 400
    response = await async_client.patch(
        "/api/v1/git-backup/config",
        json={
            "enabled": False,
            "schedule_enabled": False,
            "secret_actions": {"access_token": "clear"},
        },
    )
    assert response.status_code == 200 and response.json()["has_token"] is False


@pytest.mark.parametrize(
    "provider,base",
    [
        ("github", "https://github.com"),
        ("gitlab", "https://gitlab.example"),
        ("gitea", "https://gitea.example/subpath"),
        ("forgejo", "https://forgejo.example/subpath"),
    ],
)
async def test_git_provider_scope_and_replacement(async_client, git_network, provider, base):
    response = await async_client.post(
        "/api/v1/git-backup/config",
        json={
            "provider": provider,
            "repository_url": base + "/owner/private",
            "access_token": "CANARY_PROVIDER",
        },
    )
    assert response.status_code == 200, response.text
    git_network.reset_mock()
    response = await async_client.patch("/api/v1/git-backup/config", json={"repository_url": base + "/owner/other"})
    assert response.status_code == 400
    git_network.assert_not_awaited()
    response = await async_client.patch(
        "/api/v1/git-backup/config",
        json={
            "repository_url": base + "/owner/other",
            "access_token": "NEW_PROVIDER",
            "secret_actions": {"access_token": "replace"},
        },
    )
    assert response.status_code == 200, response.text
    assert git_network.await_args.args == (base + "/owner/other", "NEW_PROVIDER")


async def test_git_privacy_failure_keeps_the_complete_old_pair(async_client, git_network):
    await git_config(async_client)
    git_network.return_value = {"success": True, "is_private": False}
    response = await async_client.patch(
        "/api/v1/git-backup/config",
        json={
            "repository_url": "https://github.com/owner/public",
            "access_token": "NEW_PUBLIC_TOKEN",
            "branch": "new",
        },
    )
    assert response.status_code == 400
    response = await async_client.get("/api/v1/git-backup/config")
    assert response.json()["repository_url"].endswith("/private")
    assert response.json()["branch"] == "main"


@pytest.mark.parametrize(
    "base",
    [
        "https://user:CANARY_BASE@git.example/api",
        "https://git.example/api?token=CANARY_BASE",
        "https://git.example/api#CANARY_BASE",
        "malformed-CANARY_BASE",
    ],
)
async def test_git_base_url_secret_is_rejected_before_io(async_client, git_network, base):
    await git_config(async_client)
    git_network.reset_mock()
    for path, body in (
        ("/api/v1/git-backup/config", {"api_base_url": base}),
        (
            "/api/v1/git-backup/test",
            {"repo_url": "https://github.com/owner/private", "token": "NEW_TOKEN", "api_base_url": base},
        ),
    ):
        response = await async_client.request("POST" if path.endswith("/test") else "PATCH", path, json=body)
        assert response.status_code == 422, response.text
        assert "CANARY" not in response.text
    git_network.assert_not_awaited()


async def test_git_legacy_private_base_is_opaque_and_omission_keeps_it(async_client, git_network, db_session):
    await git_config(async_client)
    row = await db_session.scalar(select(GitBackupConfig))
    row.api_base_url = "https://user:CANARY_BASE@git.example/api?token=CANARY_BASE"
    await db_session.commit()
    response = await async_client.get("/api/v1/git-backup/config")
    assert "CANARY" not in response.text
    assert response.json()["api_base_url"] is None and response.json()["api_base_url_private"] is True
    response = await async_client.patch("/api/v1/git-backup/config", json={"branch": "renamed"})
    assert response.status_code == 200, response.text
    response = await async_client.post(
        "/api/v1/git-backup/test",
        json={"repo_url": "https://github.com/owner/private", "secret_actions": {"token": "keep"}},
    )
    assert response.status_code == 200, response.text
    assert git_network.await_args.kwargs["api_base_url"] == "https://user:CANARY_BASE@git.example/api?token=CANARY_BASE"
    response = await async_client.post(
        "/api/v1/git-backup/config",
        json={
            "repository_url": "https://github.com/owner/private",
            "access_token": "ROTATED",
            "secret_actions": {"access_token": "replace"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["api_base_url_private"] is True and "CANARY" not in response.text
    await db_session.refresh(row)
    assert row.api_base_url == "https://user:CANARY_BASE@git.example/api?token=CANARY_BASE"
    assert row.access_token == "ROTATED"


@pytest.mark.parametrize("success", [False, True])
async def test_git_peer_echoes_never_reach_test_status_or_history(
    async_client, git_network, db_session, monkeypatch, success
):
    from backend.app.models.git_backup import GitBackupLog

    await git_config(async_client)
    row = await db_session.scalar(select(GitBackupConfig))
    row.last_backup_status = "failed"
    row.last_backup_message = "peer echoed CANARY_GIT and CANARY_UNKNOWN"
    db_session.add(
        GitBackupLog(
            config_id=row.id, status="failed", trigger="manual", error_message="peer CANARY_GIT CANARY_UNKNOWN"
        )
    )
    await db_session.commit()
    git_network.return_value = {
        "success": success,
        "message": "peer CANARY_GIT CANARY_UNKNOWN",
        "repo_name": "CANARY_UNKNOWN",
        "permissions": {"token": "CANARY_GIT"},
    }
    monkeypatch.setattr(
        git_backup_service,
        "run_backup",
        AsyncMock(return_value={"success": success, "message": "CANARY_GIT CANARY_UNKNOWN"}),
    )
    for method, path, body in (
        ("POST", "/test", {"repo_url": "https://github.com/owner/private", "token": "CANARY_GIT"}),
        ("POST", "/test-stored", None),
        ("POST", "/run", None),
        ("GET", "/config", None),
        ("GET", "/logs", None),
        ("GET", "/status", None),
    ):
        response = await async_client.request(method, "/api/v1/git-backup" + path, json=body)
        assert response.status_code == 200, response.text
        assert "CANARY" not in response.text, (path, response.text)


async def test_git_network_runs_without_transaction_and_race_is_rejected(async_client, git_network, monkeypatch):
    from backend.app.core.credential_config import lock_credential_config
    from backend.app.core.database import async_session

    await git_config(async_client)

    async def race(*args, **kwargs):
        async with async_session() as other:
            await lock_credential_config(other)
            row = await other.scalar(select(GitBackupConfig))
            row.branch = "concurrent"
            await other.commit()
        return {"success": True, "message": "Connected", "is_private": True}

    monkeypatch.setattr(git_backup_service, "test_connection", race)
    response = await async_client.patch("/api/v1/git-backup/config", json={"access_token": "NEW_TOKEN"})
    assert response.status_code == 409, response.text
    response = await async_client.get("/api/v1/git-backup/config")
    assert response.json()["branch"] == "concurrent"


@pytest.mark.parametrize(
    "actions,extra",
    [
        ({"access_token": "keep"}, {"access_token": "CANARY_CONFLICT"}),
        ({"access_token": "clear"}, {"access_token": None}),
        ({"access_token": "replace"}, {}),
        ({"branch": "keep"}, {}),
        ({"access_token": "guess"}, {}),
    ],
)
async def test_git_invalid_action_never_reaches_network(async_client, git_network, actions, extra):
    await git_config(async_client)
    git_network.reset_mock()
    response = await async_client.patch("/api/v1/git-backup/config", json={"secret_actions": actions, **extra})
    assert response.status_code == 422
    assert "CANARY" not in response.text
    git_network.assert_not_awaited()


async def test_oidc_scope_and_disabled_clear(async_client):
    response = await async_client.post(
        "/api/v1/auth/oidc/providers",
        json={
            "name": "Private IdP",
            "issuer_url": "https://identity.example",
            "client_id": "app",
            "client_secret": "CANARY_OIDC",
            "secret_actions": {"client_secret": "replace"},
        },
    )
    assert response.status_code == 201, response.text
    provider = response.json()
    assert provider["client_secret_configured"] is True and "CANARY" not in response.text
    path = f"/api/v1/auth/oidc/providers/{provider['id']}"
    for change in ({"issuer_url": "https://different.example"}, {"client_id": "other-app"}):
        response = await async_client.put(path, json=change)
        assert response.status_code == 400, response.text
    response = await async_client.put(path, json={"name": "Renamed", "secret_actions": {"client_secret": "keep"}})
    assert response.status_code == 200
    response = await async_client.put(path, json={"is_enabled": False, "secret_actions": {"client_secret": "clear"}})
    assert response.status_code == 200 and response.json()["client_secret_configured"] is False
    response = await async_client.put(path, json={"is_enabled": True})
    assert response.status_code == 400


async def control_only(client, db):
    user = User(
        username="control_only",
        role="user",
        is_active=True,
        groups=[
            Group(
                name="Control only",
                permissions=["smart_plugs:control", "smart_plugs:read"],
            )
        ],
    )
    db.add(user)
    await db.commit()
    client.headers["Authorization"] = "Bearer " + await mint_test_access(db, username=user.username)


async def test_tasmota_read_keep_rebind_and_saved_test_authority(async_client, db_session, monkeypatch):
    from backend.app.services.tasmota import tasmota_service

    response = await async_client.post(
        "/api/v1/smart-plugs/",
        json={
            "name": "Tasmota",
            "ip_address": "192.168.1.90",
            "username": "account",
            "password": "CANARY_TASMOTA",
        },
    )
    assert response.status_code == 200, response.text
    plug = response.json()
    assert "password" not in plug and plug["configured_secrets"]["password"] is True
    path = f"/api/v1/smart-plugs/{plug['id']}"
    response = await async_client.patch(path, json={"ip_address": "192.168.1.91"})
    assert response.status_code == 400
    response = await async_client.patch(path, json={"name": "Renamed"})
    assert response.status_code == 200
    network = AsyncMock(return_value={"success": True, "state": "ON"})
    monkeypatch.setattr(tasmota_service, "test_connection", network)
    response = await async_client.post(
        "/api/v1/smart-plugs/test-connection",
        json={
            "plug_id": plug["id"],
            "ip_address": "192.168.1.90",
            "username": "account",
            "secret_actions": {"password": "keep"},
        },
    )
    assert response.status_code == 200, response.text
    assert network.await_args.args[-1] == "CANARY_TASMOTA"
    network.reset_mock()
    await control_only(async_client, db_session)
    response = await async_client.post(
        "/api/v1/smart-plugs/test-connection",
        json={
            "plug_id": plug["id"],
            "ip_address": "192.168.1.90",
            "username": "account",
        },
    )
    assert response.status_code == 403
    network.assert_not_awaited()
    response = await async_client.post(
        "/api/v1/smart-plugs/test-connection",
        json={
            "ip_address": "192.168.1.91",
            "password": "NEW_EXPLICIT",
        },
    )
    assert response.status_code == 200


async def test_rest_opaque_reads_scope_and_draft_use(async_client, monkeypatch, db_session):
    from backend.app.services.rest_smart_plug import rest_smart_plug_service

    response = await async_client.post(
        "/api/v1/smart-plugs/",
        json={
            "name": "REST",
            "plug_type": "rest",
            "rest_on_url": "http://bridge.example/CANARY_ON",
            "rest_off_url": "http://bridge.example/CANARY_OFF",
            "rest_method": "POST",
            "rest_headers": '{"Authorization":"CANARY_HEADER"}',
            "rest_on_body": '{"any":"CANARY_BODY"}',
        },
    )
    assert response.status_code == 200, response.text
    plug = response.json()
    for path in ("/api/v1/smart-plugs/", f"/api/v1/smart-plugs/{plug['id']}"):
        response = await async_client.get(path)
        assert response.status_code == 200 and "CANARY" not in response.text
    for field in ("rest_on_url", "rest_off_url", "rest_status_url", "rest_power_url", "rest_energy_url", "username"):
        response = await async_client.patch(
            f"/api/v1/smart-plugs/{plug['id']}", json={field: "http://other.example/status"}
        )
        assert response.status_code == 400, (field, response.text)
    from backend.app.api.routes import smart_plugs as routes

    lock = routes.lock_credential_config
    captured = []

    async def record(db):
        captured.append(db)
        await lock(db)

    async def outbound(*args, **kwargs):
        assert captured and not captured[-1].in_transaction()
        return {"success": True}

    monkeypatch.setattr(routes, "lock_credential_config", record)
    network = AsyncMock(side_effect=outbound)
    monkeypatch.setattr(rest_smart_plug_service, "test_connection", network)
    response = await async_client.post(
        "/api/v1/smart-plugs/rest/test-connection",
        json={
            "plug_id": plug["id"],
            "draft": {"name": "Unsaved"},
        },
    )
    assert response.status_code == 200, response.text
    assert network.await_args.args == ("http://bridge.example/CANARY_ON", "POST", '{"Authorization":"CANARY_HEADER"}')
    assert network.await_args.kwargs == {"body": '{"any":"CANARY_BODY"}'}
    stored = await db_session.scalar(select(SmartPlug).where(SmartPlug.id == plug["id"]))
    assert stored.name == "REST" and stored.rest_on_body == '{"any":"CANARY_BODY"}'
    network.reset_mock()
    response = await async_client.post(
        "/api/v1/smart-plugs/rest/test-connection",
        json={
            "plug_id": plug["id"],
            "draft": {"rest_method": "GET"},
        },
    )
    assert response.status_code == 400
    network.assert_not_awaited()


async def test_ha_environment_token_cannot_follow_draft_destination(async_client, monkeypatch):
    from backend.app.services.homeassistant import homeassistant_service

    monkeypatch.setenv("HA_TOKEN", "CANARY_ENV_HA")
    monkeypatch.delenv("HA_URL", raising=False)
    network = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(homeassistant_service, "test_connection", network)
    response = await async_client.post(
        "/api/v1/smart-plugs/ha/test-connection",
        json={
            "url": "https://different.example",
            "token": "NEW_DB_VALUE",
            "secret_actions": {"ha_token": "replace"},
        },
    )
    assert response.status_code == 400, response.text
    network.assert_not_awaited()
    assert "CANARY" not in response.text


async def test_rest_independent_capability_rotation_and_runtime_get_fallback(async_client, monkeypatch, db_session):
    from backend.app.services.rest_smart_plug import rest_smart_plug_service

    response = await async_client.post(
        "/api/v1/smart-plugs/",
        json={
            "name": "Independent URLs",
            "plug_type": "rest",
            "rest_on_url": "http://bridge.example/CANARY_ON",
            "rest_off_url": "http://bridge.example/CANARY_OFF",
            "rest_status_url": "http://bridge.example/CANARY_STATUS",
            "rest_method": "POST",
        },
    )
    assert response.status_code == 200, response.text
    plug_id = response.json()["id"]
    response = await async_client.patch(
        f"/api/v1/smart-plugs/{plug_id}",
        json={
            "rest_on_url": "http://other.example/NEW_ON",
            "secret_actions": {"rest_on_url": "replace", "rest_off_url": "keep"},
        },
    )
    assert response.status_code == 200, response.text
    stored = await db_session.scalar(select(SmartPlug).where(SmartPlug.id == plug_id))
    assert (
        stored.rest_on_url == "http://other.example/NEW_ON"
        and stored.rest_off_url == "http://bridge.example/CANARY_OFF"
    )
    network = AsyncMock(return_value={"success": True, "error": "peer CANARY_STATUS"})
    monkeypatch.setattr(rest_smart_plug_service, "test_connection", network)
    for endpoint in ("rest_status_url", "rest_power_url", "rest_energy_url"):
        response = await async_client.post(
            "/api/v1/smart-plugs/rest/test-connection", json={"plug_id": plug_id, "draft": {}, "endpoint": endpoint}
        )
        assert response.status_code == 200 and "CANARY" not in response.text
        assert network.await_args.args == ("http://bridge.example/CANARY_STATUS", "GET", None)


@pytest.mark.parametrize(
    "kind,field,values",
    [
        ("tasmota", "password", {"ip_address": "192.168.1.88", "username": "account", "password": "CANARY_PASSWORD"}),
        (
            "rest",
            "rest_headers",
            {"rest_on_url": "http://bridge.example/on", "rest_headers": '{"Authorization":"CANARY_HEADER"}'},
        ),
        ("rest", "rest_on_body", {"rest_on_url": "http://bridge.example/on", "rest_on_body": '{"auth":"CANARY_BODY"}'}),
    ],
)
async def test_removing_auth_requires_explicit_disabled_mode(
    async_client, db_session, monkeypatch, kind, field, values
):
    from backend.app.services.rest_smart_plug import rest_smart_plug_service
    from backend.app.services.tasmota import tasmota_service

    network = AsyncMock(side_effect=AssertionError("configuration refusal reached a device"))
    monkeypatch.setattr(rest_smart_plug_service, "test_connection", network)
    monkeypatch.setattr(tasmota_service, "test_connection", network)
    response = await async_client.post("/api/v1/smart-plugs/", json={"name": "Safe clear", "plug_type": kind, **values})
    assert response.status_code == 200, response.text
    plug_id = response.json()["id"]
    response = await async_client.patch(
        f"/api/v1/smart-plugs/{plug_id}", json={"name": "Should not persist", "secret_actions": {field: "clear"}}
    )
    assert response.status_code == 400, response.text
    stored = await db_session.scalar(select(SmartPlug).where(SmartPlug.id == plug_id))
    assert stored.name == "Safe clear" and getattr(stored, field) == values[field]
    response = await async_client.patch(
        f"/api/v1/smart-plugs/{plug_id}", json={"enabled": False, "secret_actions": {field: "clear"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False and response.json()["configured_secrets"][field] is False
    network.assert_not_awaited()


@pytest.mark.parametrize("success", [True, False])
async def test_ha_peer_response_is_not_a_credential_read(async_client, monkeypatch, success):
    from backend.app.services.homeassistant import homeassistant_service

    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr(
        homeassistant_service,
        "test_connection",
        AsyncMock(return_value={"success": success, "message": "CANARY_NEW_HA", "error": "CANARY_NEW_HA"}),
    )
    response = await async_client.post(
        "/api/v1/smart-plugs/ha/test-connection", json={"url": "http://ha.example", "token": "CANARY_NEW_HA"}
    )
    assert response.status_code == 200, response.text
    assert "CANARY" not in response.text


@pytest.mark.parametrize("wire,field", [("url", "ha_url"), ("token", "ha_token")])
@pytest.mark.parametrize("action", ["keep", "clear"])
async def test_ha_explicit_null_cannot_conflict_with_action(async_client, monkeypatch, wire, field, action):
    from backend.app.services.homeassistant import homeassistant_service

    network = AsyncMock(side_effect=AssertionError("invalid action reached network"))
    monkeypatch.setattr(homeassistant_service, "test_connection", network)
    response = await async_client.post(
        "/api/v1/smart-plugs/ha/test-connection", json={wire: None, "secret_actions": {field: action}}
    )
    assert response.status_code == 422, response.text
    network.assert_not_awaited()


async def test_mqtt_plug_uses_serialized_latest_pair_apply_outside_transaction(async_client, monkeypatch):
    from backend.app.api.routes import settings as settings_routes
    from backend.app.services.mqtt_relay import mqtt_relay

    service = MagicMock()
    service.is_configured.side_effect = [False, True]
    monkeypatch.setattr(mqtt_relay, "_smart_plug_service", service)

    async def apply(db, *, smart_plug_only=False):
        assert smart_plug_only is True and not db.in_transaction()

    applied = AsyncMock(side_effect=apply)
    monkeypatch.setattr(settings_routes, "_apply_mqtt_runtime", applied)
    response = await async_client.post(
        "/api/v1/smart-plugs/",
        json={
            "name": "Independent MQTT",
            "plug_type": "mqtt",
            "mqtt_power_topic": "meter/power",
        },
    )
    assert response.status_code == 200, response.text
    applied.assert_awaited_once()


@pytest.mark.parametrize("success", [True, False])
async def test_git_real_run_persists_peer_status_but_projects_safe_reads(
    async_client, test_engine, monkeypatch, success
):
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.services import git_backup as service_module

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(service_module, "async_session", session_factory)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "private": True,
                    "full_name": "CANARY_GIT",
                    "permissions": {"push": True},
                },
            )
        )
    ) as provider_client:
        monkeypatch.setattr(git_backup_service, "_get_client", AsyncMock(return_value=provider_client))
        monkeypatch.setattr(
            git_backup_service, "_collect_backup_data", AsyncMock(return_value={"test.json": {"safe": True}})
        )
        monkeypatch.setattr(
            git_backup_service,
            "_push_to_provider",
            AsyncMock(
                return_value={
                    "status": "success" if success else "failed",
                    "message": "CANARY_GIT peer echo",
                    "error": "CANARY_GIT peer echo",
                }
            ),
        )
        await git_config(async_client)
        response = await async_client.post("/api/v1/git-backup/test-stored")
        assert response.status_code == 200 and "CANARY" not in response.text
        response = await async_client.post("/api/v1/git-backup/run")
        assert response.status_code == 200, response.text
        assert response.json()["success"] is success and "CANARY" not in response.text
        for path in ("/config", "/status", "/logs"):
            response = await async_client.get("/api/v1/git-backup" + path)
            assert response.status_code == 200 and "CANARY" not in response.text, (path, response.text)


async def test_ha_saved_token_requires_editor_and_cannot_rebind(async_client, db_session, monkeypatch):
    from backend.app.models.settings import Settings
    from backend.app.services.homeassistant import homeassistant_service

    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    db_session.add_all([Settings(key="ha_url", value="http://ha.example"), Settings(key="ha_token", value="CANARY_HA")])
    await db_session.commit()
    network = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(homeassistant_service, "test_connection", network)
    response = await async_client.post("/api/v1/smart-plugs/ha/test-connection", json={"url": "http://other.example"})
    assert response.status_code == 400, response.text
    network.assert_not_awaited()
    response = await async_client.post(
        "/api/v1/smart-plugs/ha/test-connection",
        json={"url": "http://ha.example", "secret_actions": {"ha_token": "keep"}},
    )
    assert response.status_code == 200, response.text
    assert network.await_args.args == ("http://ha.example", "CANARY_HA")
    network.reset_mock()
    await control_only(async_client, db_session)
    response = await async_client.post("/api/v1/smart-plugs/ha/test-connection", json={"url": "http://ha.example"})
    assert response.status_code == 403
    network.assert_not_awaited()


async def test_ia1_opaque_environment_ha_url_requires_real_admin_with_new_token(async_client, db_session, monkeypatch):
    from backend.app.services.homeassistant import homeassistant_service

    monkeypatch.setenv("HA_URL", "http://account:CANARY_ENV_URL@ha.example/base?token=CANARY_ENV_URL")
    monkeypatch.delenv("HA_TOKEN", raising=False)
    user = User(
        username="ha_delegate",
        role="user",
        is_active=True,
        groups=[
            Group(
                name="HA delegated permissions",
                permissions=["settings:update", "smart_plugs:control"],
            )
        ],
    )
    db_session.add(user)
    await db_session.commit()
    async_client.headers["Authorization"] = "Bearer " + await mint_test_access(db_session, username=user.username)
    network = AsyncMock(side_effect=AssertionError("non-admin reached private environment destination"))
    monkeypatch.setattr(homeassistant_service, "test_connection", network)
    response = await async_client.post(
        "/api/v1/smart-plugs/ha/test-connection",
        json={
            "url": "http://provided.example",
            "token": "NEW_EXPLICIT_TOKEN",
        },
    )
    assert response.status_code == 403, response.text
    assert "CANARY" not in response.text
    network.assert_not_awaited()
