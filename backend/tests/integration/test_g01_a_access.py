"""Exercise custom-role gates through HTTP, including wrappers and safe no-op forms."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError

from backend.app.core.auth import create_access_token, get_password_hash
from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.oidc_provider import OIDCProvider
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.tests.auth_helpers import mint_test_access

pytestmark = pytest.mark.asyncio


async def custom_headers(db):
    group = Group(name="G01 custom administrator permissions", permissions=[p.value for p in Permission])
    user = User(username="g01_custom", role="user", is_active=True, password_hash="unused", groups=[group])
    db.add(user)
    await db.commit()
    return {"Authorization": f"Bearer {await mint_test_access(db, username=user.username)}"}


ADMIN_DOORS = [
    ("GET", "/settings/backup"),
    ("POST", "/settings/restore"),
    ("POST", "/settings/reset"),
    ("PUT", "/settings/spoolman"),
    ("PUT", "/settings/virtual-printer"),
    ("GET", "/local-backup/path-check"),
    ("POST", "/local-backup/run"),
    ("GET", "/local-backup/backups"),
    ("GET", "/local-backup/backups/backup.zip/download"),
    ("POST", "/local-backup/backups/backup.zip/restore"),
    ("DELETE", "/local-backup/backups/backup.zip"),
    ("GET", "/git-backup/config"),
    ("POST", "/git-backup/config"),
    ("PATCH", "/git-backup/config"),
    ("DELETE", "/git-backup/config"),
    ("POST", "/git-backup/test"),
    ("POST", "/git-backup/test-stored"),
    ("POST", "/git-backup/run"),
    ("GET", "/git-backup/logs"),
    ("DELETE", "/git-backup/logs"),
    ("GET", "/git-backup/commits"),
    ("GET", "/git-backup/restore/preview"),
    ("POST", "/git-backup/restore"),
    ("GET", "/auth/smtp"),
    ("POST", "/auth/smtp"),
    ("POST", "/auth/smtp/test"),
    ("POST", "/auth/ldap/test"),
    ("GET", "/auth/ldap/search"),
    ("POST", "/auth/ldap/provision"),
    ("DELETE", "/auth/2fa/admin/99999"),
    ("GET", "/auth/oidc/providers/all"),
    ("POST", "/auth/oidc/providers"),
    ("PUT", "/auth/oidc/providers/99999"),
    ("DELETE", "/auth/oidc/providers/99999"),
    ("DELETE", "/auth/oidc/providers/99999/icon"),
    ("POST", "/spoolman/connect"),
    ("POST", "/spoolman/disconnect"),
    ("POST", "/virtual-printers"),
    ("PUT", "/virtual-printers/99999"),
    ("DELETE", "/virtual-printers/99999"),
    ("POST", "/obico/test-connection"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_DOORS)
async def test_permission_holder_is_not_admin(async_client, db_session, monkeypatch, method, path):
    from backend.app.api.routes import settings as route
    from backend.app.services.git_backup import git_backup_service
    from backend.app.services.local_backup import local_backup_service

    capture = AsyncMock(side_effect=AssertionError("denied request reached backup capture"))
    network = AsyncMock(side_effect=AssertionError("denied request reached outbound work"))
    local_read = AsyncMock(side_effect=AssertionError("denied request read deployment paths"))
    monkeypatch.setattr(route, "create_backup_zip", capture)
    monkeypatch.setattr(git_backup_service, "test_connection", network)
    monkeypatch.setattr(local_backup_service, "_load_settings", local_read)
    headers = await custom_headers(db_session)
    response = await async_client.request(method, "/api/v1" + path, headers=headers, json={})
    assert response.status_code == 403, response.text
    capture.assert_not_called()
    network.assert_not_called()
    local_read.assert_not_called()
    assert await db_session.scalar(select(Settings.id).where(Settings.key == "g01_sentinel")) is None


def protected_changes():
    # Independent policy fixture: omitting any one of these fields from the
    # production guard still runs its denial test.
    values = {
        "local_login_enabled": False,
        "session_max_hours": 13,
        "telegram_registration_open": True,
        "mcp_enabled": True,
        "mcp_allow_http": True,
        "mcp_endpoint_url": "https://new.example/mcp",
        "mcp_allowed_hosts": ["new.example"],
        "mcp_allowed_origins": ["https://new.example"],
        "mqtt_enabled": True,
        "mqtt_broker": "new.example",
        "mqtt_port": 1884,
        "mqtt_username": "new",
        "mqtt_password": "secret-canary",
        "mqtt_topic_prefix": "new",
        "mqtt_use_tls": True,
        "ldap_enabled": True,
        "ldap_server_url": "ldaps://new.example",
        "ldap_bind_dn": "cn=new",
        "ldap_bind_password": "secret-canary",
        "ldap_search_base": "dc=new",
        "ldap_user_filter": "(uid={username})",
        "ldap_security": "ldaps",
        "ldap_group_mapping": '{"cn=new":"Viewers"}',
        "ldap_auto_provision": True,
        "ldap_default_group": "Operators",
        "ha_enabled": True,
        "ha_url": "https://new.example",
        "ha_token": "secret-canary",
        "prometheus_enabled": True,
        "prometheus_token": "secret-canary",
        "local_backup_enabled": True,
        "local_backup_schedule": "hourly",
        "local_backup_time": "04:00",
        "local_backup_retention": 7,
        "local_backup_path": "/new",
        "virtual_printer_enabled": True,
        "virtual_printer_access_code": "secret-canary",
        "virtual_printer_mode": "proxy",
        "virtual_printer_archive_name_source": "filename",
        "docker_compose_dir": "/new",
        "external_url": "https://new.example",
        "zigbee_enabled": True,
        "zigbee_transport": "usb",
        "zigbee_path": "/new",
        "spoolman_enabled": True,
        "spoolman_url": "https://new.example",
        "spoolman_sync_mode": "pull",
        "orcaslicer_api_url": "https://new.example",
        "bambu_studio_api_url": "https://new.example",
        "obico_enabled": True,
        "obico_ml_url": "https://new.example",
        "obico_ml_token": "secret-canary",
    }
    return list(values.items())


@pytest.mark.parametrize(("key", "value"), protected_changes())
@pytest.mark.parametrize("method", ["patch", "put"])
async def test_protected_change_rejects_whole_mixed_form(async_client, db_session, key, value, method):
    headers = await custom_headers(db_session)
    response = await async_client.request(
        method, "/api/v1/settings/", headers=headers, json={key: value, "currency": "EUR"}
    )
    assert response.status_code == 403, response.text
    assert await db_session.scalar(select(Settings.id).where(Settings.key.in_([key, "currency"]))) is None


async def test_unchanged_policy_and_ordinary_preference_save(async_client, db_session):
    headers = await custom_headers(db_session)
    response = await async_client.patch(
        "/api/v1/settings/", headers=headers, json={"local_login_enabled": True, "currency": "EUR"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["currency"] == "EUR"


async def test_unknown_write_and_validation_do_not_echo_secret(async_client):
    secret = "g01-opaque-secret-must-not-be-in-validation"
    response = await async_client.patch("/api/v1/settings/", json={secret: secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


async def test_public_oidc_metadata_and_private_admin_config(async_client, db_session):
    provider = OIDCProvider(
        name="G01 identity", issuer_url="https://issuer.example/private", client_id="private-client"
    )
    provider.client_secret = "private-secret"
    db_session.add(provider)
    await db_session.commit()
    response = await async_client.get("/api/v1/auth/oidc/providers", headers={"Authorization": ""})
    assert response.status_code == 200
    assert response.json() == [{"id": provider.id, "name": provider.name, "has_icon": False, "is_autologin": False}]
    assert "private" not in response.text
    private = await async_client.get("/api/v1/auth/oidc/providers/all")
    assert private.status_code == 200 and private.json()[0]["issuer_url"] == provider.issuer_url


async def test_git_test_requires_json_and_keeps_admin_operation(async_client, monkeypatch):
    from backend.app.services.git_backup import git_backup_service

    send = AsyncMock(return_value={"success": True, "message": "ok", "is_private": True})
    monkeypatch.setattr(git_backup_service, "test_connection", send)
    payload = {"repo_url": "https://github.com/example/private", "token": "g01-body-secret", "provider": "github"}
    response = await async_client.post("/api/v1/git-backup/test", json=payload)
    assert response.status_code == 200, response.text
    send.assert_awaited_once_with(payload["repo_url"], payload["token"], provider="github", api_base_url=None)
    send.reset_mock()
    response = await async_client.post("/api/v1/git-backup/test", params=payload)
    assert response.status_code == 422
    assert payload["token"] not in response.text
    send.assert_not_called()


async def test_missing_mfa_target_does_not_create_orphan_setting(async_client, db_session):
    response = await async_client.request(
        "DELETE", "/api/v1/auth/2fa/admin/99999", json={"admin_password": "Test_AdminPass1!"}
    )
    assert response.status_code == 404
    assert await db_session.scalar(select(Settings.id).where(Settings.key.like("%99999%"))) is None


async def test_email_omitted_keeps_and_empty_or_null_clear(async_client, db_session):
    user = User(
        username="g01_email", role="user", email="email@example.com", password_hash=get_password_hash("SafePass123!")
    )
    db_session.add(user)
    await db_session.commit()
    path = f"/api/v1/users/{user.id}"
    assert (await async_client.patch(path, json={})).json()["email"] == "email@example.com"
    assert (await async_client.patch(path, json={"email": ""})).json()["email"] is None
    assert (await async_client.patch(path, json={"email": "new@example.com"})).json()["email"] == "new@example.com"
    assert (await async_client.patch(path, json={"email": None})).json()["email"] is None


@pytest.mark.parametrize("kind", ["jwt", "key"])
async def test_auth_database_failure_is_retryable_not_invalid(async_client, monkeypatch, kind):
    from backend.app.core import auth

    failure = OperationalError("secret-sql-canary", {}, RuntimeError("private-driver-detail"))
    monkeypatch.setattr(
        auth, f"_validate_{'jwt' if kind == 'jwt' else 'api_key'}_authority", AsyncMock(side_effect=failure)
    )
    headers = None if kind == "jwt" else {"X-API-Key": "bd_test_key"}
    response = await async_client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    assert "secret-sql-canary" not in response.text and "private-driver-detail" not in response.text


async def test_handler_exception_is_not_rewritten_to_credential_failure(async_client, monkeypatch):
    from backend.app.api.routes import settings as route

    monkeypatch.setattr(route, "get_homeassistant_settings", AsyncMock(side_effect=RuntimeError("handler bug")))
    with pytest.raises(RuntimeError, match="handler bug"):
        await async_client.get("/api/v1/settings/")


@pytest.mark.parametrize("route", ["login", "refresh"])
async def test_public_auth_database_outage_is_retryable(async_client, db_session, monkeypatch, route):
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.core.database import get_db
    from backend.app.main import app

    async def unavailable_db():
        # Preserve the real transaction owner used by C's drain accounting;
        # fail the actual first database operation, not lifecycle bookkeeping.
        async with AsyncSession(bind=db_session.bind) as broken:
            broken.execute = AsyncMock(side_effect=OperationalError("private-sql", {}, RuntimeError()))
            yield broken

    app.dependency_overrides[get_db] = unavailable_db
    payload = {"username": "outage", "password": "SafePass123!"} if route == "login" else None
    response = await async_client.post(
        f"/api/v1/auth/{route}", json=payload, headers={"Authorization": "", "Cookie": "bamdude_refresh=opaque"}
    )
    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    assert "private-sql" not in response.text


async def test_successful_ldap_with_local_database_failure_is_not_bad_password(async_client, monkeypatch):
    from backend.app.api.routes import auth as route
    from backend.app.services import ldap_service

    failure = OperationalError("private-ldap-sql", {}, RuntimeError())
    monkeypatch.setattr(route, "_get_ldap_settings", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(ldap_service, "parse_ldap_config", lambda _: SimpleNamespace(auto_provision=True))
    monkeypatch.setattr(ldap_service, "authenticate_ldap_user", lambda *args: SimpleNamespace(username="ldap-person"))
    monkeypatch.setattr(route, "get_user_by_username", AsyncMock(side_effect=failure))
    response = await async_client.post(
        "/api/v1/auth/login",
        json={"username": "ldap-person", "password": "SafePass123!"},
        headers={"Authorization": ""},
    )
    assert response.status_code == 503, response.text
    assert "private-ldap-sql" not in response.text


@pytest.mark.parametrize("kind", ["jwt", "key", "ldap"])
async def test_auth_programming_error_remains_a_server_error(async_client, monkeypatch, kind):
    from backend.app.api.routes import auth as route
    from backend.app.core import auth
    from backend.app.services import ldap_service

    failure = ProgrammingError("broken query", {}, RuntimeError("programming defect"))
    if kind == "ldap":
        monkeypatch.setattr(route, "_get_ldap_settings", AsyncMock(return_value={"enabled": True}))
        monkeypatch.setattr(ldap_service, "parse_ldap_config", lambda _: SimpleNamespace(auto_provision=True))
        monkeypatch.setattr(ldap_service, "authenticate_ldap_user", lambda *args: SimpleNamespace(username="ldap-bug"))
        monkeypatch.setattr(route, "get_user_by_username", AsyncMock(side_effect=failure))
        with pytest.raises(ProgrammingError):
            await async_client.post(
                "/api/v1/auth/login",
                json={"username": "ldap-bug", "password": "SafePass123!"},
                headers={"Authorization": ""},
            )
    else:
        monkeypatch.setattr(
            auth, f"_validate_{'jwt' if kind == 'jwt' else 'api_key'}_authority", AsyncMock(side_effect=failure)
        )
        with pytest.raises(ProgrammingError):
            await async_client.get("/api/v1/auth/me", headers=None if kind == "jwt" else {"X-API-Key": "bd_test_key"})


async def test_safe_preferences_without_settings_permission(async_client, db_session):
    group = Group(name="Only printer reader", permissions=[Permission.PRINTERS_READ.value])
    user = User(username="g01-minimal-ui", role="user", is_active=True, password_hash="unused", groups=[group])
    db_session.add_all([user, Settings(key="mqtt_password", value="ui-secret-canary")])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {await mint_test_access(db_session, username=user.username)}"}
    response = await async_client.get("/api/v1/settings/ui-preferences", headers=headers)
    assert response.status_code == 200, response.text
    assert "ui-secret-canary" not in response.text and "mqtt_password" not in response.json()
    assert "time_format" in response.json()
    assert (await async_client.get("/api/v1/settings/", headers=headers)).status_code == 403


@pytest.mark.parametrize("method", ["put", "patch"])
async def test_unchanged_policy_is_never_rewritten_after_an_admin_change(async_client, db_session, monkeypatch, method):
    from backend.app.api.routes import settings as route

    headers = await custom_headers(db_session)
    check = route._enforce_admin_settings

    async def change_policy_after_check(db, changes, user):
        await check(db, changes, user)
        await route.set_setting(db, "local_login_enabled", "false")

    monkeypatch.setattr(route, "_enforce_admin_settings", change_policy_after_check)
    response = await async_client.request(
        method, "/api/v1/settings/", headers=headers, json={"local_login_enabled": True, "currency": "EUR"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["local_login_enabled"] is False
    assert response.json()["currency"] == "EUR"


@pytest.mark.parametrize(
    "path",
    [
        "/camwall/printers?token=opaque",
        "/printers/1/overlay-status?token=opaque",
        "/printers/1/camera/stream?token=opaque",
        "/monitor/kiosk/snapshot",
    ],
)
async def test_kiosk_auth_database_failure_is_safe_503(async_client, monkeypatch, path):
    from backend.app.services import long_lived_tokens

    monkeypatch.setattr(
        long_lived_tokens, "verify_token", AsyncMock(side_effect=OperationalError("kiosk-secret", {}, RuntimeError()))
    )
    # Monitor imports this validator directly; the other doors load it lazily.
    monkeypatch.setattr("backend.app.api.routes.monitor.verify_token", long_lived_tokens.verify_token)
    # Camera headers choose ordinary auth without falling back to a query token.
    request = async_client.build_request("GET", "/api/v1" + path, headers={"Authorization": "Bearer opaque"})
    if "/camera/" in path:
        request.headers.pop("Authorization", None)  # Also remove the fixture client's default JWT.
    response = await async_client.send(request)
    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    assert "kiosk-secret" not in response.text


async def test_canonical_ldap_admin_can_reset_an_existing_users_2fa(async_client, db_session):
    from backend.app.models.user_totp import UserTOTP

    admins = await db_session.scalar(select(Group).where(Group.name == "Administrators"))
    actor = User(username="g01-ldap-admin", role="user", auth_source="ldap", is_active=True, groups=[admins])
    target = User(username="g01-target", role="user", auth_source="local", is_active=True)
    db_session.add_all([actor, target])
    await db_session.flush()
    totp = UserTOTP(user_id=target.id, is_enabled=True)
    totp.secret = "TESTSECRET"
    db_session.add(totp)
    await db_session.commit()
    response = await async_client.request(
        "DELETE",
        f"/api/v1/auth/2fa/admin/{target.id}",
        json={},
        headers={"Authorization": f"Bearer {await mint_test_access(db_session, username=actor.username)}"},
    )
    assert response.status_code == 200, response.text
    assert await db_session.scalar(select(UserTOTP.id).where(UserTOTP.user_id == target.id)) is None


async def test_valid_admin_owned_api_key_does_not_become_a_human_admin(async_client):
    created = await async_client.post("/api/v1/api-keys/", json={"name": "G01 admin owner", "can_read_status": True})
    assert created.status_code == 200, created.text
    headers = {"Authorization": "", "X-API-Key": created.json()["key"]}
    assert (await async_client.get("/api/v1/printers/", headers=headers)).status_code == 200
    for path in ("/auth/smtp", "/git-backup/config", "/local-backup/backups"):
        response = await async_client.get("/api/v1" + path, headers=headers)
        assert response.status_code in {401, 403}, response.text
