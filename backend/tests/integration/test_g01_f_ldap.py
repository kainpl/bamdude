"""LDAP F login/provision authority with no external directory calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.api.routes import auth
from backend.app.models.group import Group
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import ldap_service as ldap

pytestmark = pytest.mark.asyncio


async def enable(db, **values):
    settings = {
        "ldap_enabled": "true",
        "ldap_server_url": "ldap://directory.test",
        "ldap_auto_provision": "true",
        "ldap_default_group": "Viewers",
        **values,
    }
    db.add_all(Settings(key=key, value=value) for key, value in settings.items())
    await db.commit()


async def test_new_ldap_login_provisions_once_existing_login_syncs_once_and_keeps_source(
    async_client, db_session, monkeypatch
):
    await enable(db_session)
    monkeypatch.setattr(
        ldap, "authenticate_ldap_user", lambda *args: ldap.LDAPUserInfo("f-alice", "alice@example.org", "Alice", [])
    )
    provision = AsyncMock(wraps=auth._provision_ldap_user)
    sync = AsyncMock(wraps=auth._sync_ldap_user)
    monkeypatch.setattr(auth, "_provision_ldap_user", provision)
    monkeypatch.setattr(auth, "_sync_ldap_user", sync)
    body = {"username": "f-alice", "password": "external-password"}
    response = await async_client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["auth_source"] == "ldap"
    assert response.json()["user"]["email"] == "alice@example.org"
    assert [g["name"] for g in response.json()["user"]["groups"]] == ["Viewers"]
    provision.assert_awaited_once()
    sync.assert_not_awaited()
    response = await async_client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 200, response.text
    sync.assert_awaited_once()
    assert provision.await_count == 1


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [ldap.LDAPGroupLookupError, ldap.LDAPReferralError])
async def test_incomplete_lookup_never_provisions_assigns_privileged_default_or_mutates_groups(
    async_client, db_session, monkeypatch, existing, failure
):
    await enable(db_session, ldap_default_group="Administrators", ldap_group_mapping='{"cn=users,dc=x":"Viewers"}')
    manual = Group(name="F Manual", permissions=[])
    db_session.add(manual)
    viewers = await db_session.scalar(select(Group).where(Group.name == "Viewers"))
    if existing:
        db_session.add(
            User(
                username="f-incomplete",
                auth_source="ldap",
                role="user",
                email="old@example.org",
                groups=[manual, viewers],
            )
        )
    await db_session.commit()

    def directory_failure(*args):
        raise failure()

    monkeypatch.setattr(ldap, "authenticate_ldap_user", directory_failure)
    response = await async_client.post(
        "/api/v1/auth/login", json={"username": "f-incomplete", "password": "external-password"}
    )
    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    await db_session.rollback()
    user = await db_session.scalar(
        select(User).where(User.username == "f-incomplete").options(selectinload(User.groups))
    )
    if existing:
        assert user.email == "old@example.org"
        assert {g.name for g in user.groups} == {"F Manual", "Viewers"}
    else:
        assert user is None


async def test_complete_empty_lookup_preserves_valid_default_and_manual_group_policy(
    async_client, db_session, monkeypatch
):
    await enable(db_session, ldap_default_group="Administrators", ldap_group_mapping='{"cn=users,dc=x":"Viewers"}')
    manual = Group(name="F Manual", permissions=[])
    viewers = await db_session.scalar(select(Group).where(Group.name == "Viewers"))
    user = User(username="f-empty", auth_source="ldap", role="user", groups=[manual, viewers])
    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(ldap, "authenticate_ldap_user", lambda *args: ldap.LDAPUserInfo("f-empty", None, None, []))
    response = await async_client.post(
        "/api/v1/auth/login", json={"username": "f-empty", "password": "external-password"}
    )
    assert response.status_code == 200, response.text
    assert {g["name"] for g in response.json()["user"]["groups"]} == {"F Manual", "Administrators"}


async def test_incomplete_info_guard_runs_before_email_or_group_mutation():
    db = AsyncMock()
    user = SimpleNamespace(username="alice", email="old@example.org", groups=[object()])
    original_groups = user.groups
    info = ldap.LDAPUserInfo("alice", "new@example.org", None, [], groups_complete=False)
    cfg = SimpleNamespace(group_mapping={}, default_group="Administrators")
    for operation in (
        lambda: auth._sync_ldap_user(db, user, info, cfg),
        lambda: auth._provision_ldap_user(db, info, cfg),
    ):
        with pytest.raises(HTTPException) as error:
            await operation()
        assert error.value.status_code == 503
    assert user.email == "old@example.org" and user.groups is original_groups
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "door,function", [("/auth/ldap/search?q=alice", "search_ldap_users"), ("/auth/ldap/provision", "lookup_ldap_user")]
)
async def test_admin_directory_failure_never_echoes_or_logs_driver_credentials(
    async_client, db_session, monkeypatch, caplog, door, function
):
    await enable(db_session)

    def fail(*args, **kwargs):
        raise RuntimeError("directory echoed ldap://user:CANARY_PASSWORD@directory.test")

    monkeypatch.setattr(ldap, function, fail)
    response = await (
        async_client.get("/api/v1" + door)
        if function == "search_ldap_users"
        else async_client.post("/api/v1" + door, json={"username": "alice"})
    )
    assert response.status_code == 503, response.text
    assert "CANARY" not in response.text and "CANARY" not in caplog.text


@pytest.mark.parametrize(
    "failure,expected",
    [(False, "StartTLS operation refused"), (RuntimeError("CANARY_CERT"), "TLS certificate or handshake failed")],
)
async def test_ldap_draft_test_distinguishes_transport_errors_after_transaction_release(
    async_client, db_session, monkeypatch, failure, expected
):
    await enable(db_session)
    conn = MagicMock()
    conn.result = {"result": 53} if failure is False else {"result": 0}
    if failure is False:
        conn.start_tls.return_value = False
    else:
        conn.start_tls.side_effect = failure

    def create(*args, **kwargs):
        assert not db_session.in_transaction()
        return conn

    monkeypatch.setattr(ldap, "Connection", create)
    response = await async_client.post("/api/v1/auth/ldap/test", json={"ldap_search_base": "dc=draft"})
    assert response.status_code == 200, response.text
    assert not response.json()["success"] and expected in response.json()["message"]
    assert response.json()["error"] == ("starttls_refused" if failure is False else "tls_failed")
    assert "CANARY" not in response.text
    conn.bind.assert_not_called()
    conn.unbind.assert_called_once()
    assert await db_session.scalar(select(Settings.value).where(Settings.key == "ldap_search_base")) is None
