"""FL1: successful AD domain-root references are not operation referrals.

Real ldap3 SyncStrategy processes decoded directory frames; only transport
I/O is replaced. Neither continuation references nor result10 may be followed.
"""

from unittest.mock import MagicMock

import pytest
from ldap3 import Connection as RealConnection
from ldap3.strategy.base import RESPONSE_COMPLETE
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import ldap_service as ldap

pytestmark = pytest.mark.asyncio
USER_DN = "CN=Alice,OU=Staff,DC=example,DC=com"
GROUP_DN = "CN=Printers,OU=Groups,DC=example,DC=com"
REFERENCES = [
    {"type": "searchResRef", "uri": ["ldap://ForestDnsZones.example.com/DC=ForestDnsZones,DC=example,DC=com"]},
    {"type": "searchResRef", "uri": ["ldap://DomainDnsZones.example.com/DC=DomainDnsZones,DC=example,DC=com"]},
    {"type": "searchResRef", "uri": ["ldap://example.com/CN=Configuration,DC=example,DC=com"]},
]


@pytest.fixture
async def directory(db_session, monkeypatch):
    values = {
        "ldap_enabled": "true",
        "ldap_server_url": "ldap://dc.example.com",
        "ldap_security": "starttls",
        "ldap_bind_dn": "CN=svc,DC=example,DC=com",
        "ldap_bind_password": "fake-service-password",
        "ldap_search_base": "DC=example,DC=com",
        "ldap_user_filter": "(sAMAccountName={username})",
        "ldap_auto_provision": "true",
        "ldap_default_group": "Administrators",
        "ldap_group_mapping": '{"CN=Printers,OU=Groups,DC=example,DC=com":"Viewers"}',
    }
    db_session.add_all(Settings(key=k, value=v) for k, v in values.items())
    await db_session.commit()
    state = {"result": 0, "with_groups": True, "searches": []}
    followed = MagicMock(side_effect=AssertionError("unowned referral was followed"))

    def factory(*args, **kwargs):
        conn = RealConnection(*args, **kwargs)
        conn.open = lambda **kw: None
        conn.start_tls = lambda **kw: True
        conn.bind = lambda **kw: True
        conn.unbind = lambda: True
        conn.refresh_server_info = lambda: None
        conn.strategy.do_operation_on_referral = followed

        def search(search_base, search_filter, attributes=None, **kw):
            state["searches"].append(search_filter)
            attrs = {"sAMAccountName": ["fl-alice"], "mail": ["alice@example.com"]}
            if state["with_groups"]:
                attrs["memberOf"] = [GROUP_DN]
            user = {
                "type": "searchResEntry",
                "dn": USER_DN,
                "attributes": attrs,
                "raw_attributes": {k: [v.encode() for v in vs] for k, vs in attrs.items()},
            }
            done = {
                "type": "searchResDone",
                "result": state["result"],
                "description": "success",
                "dn": "",
                "message": "",
                "referrals": ["ldap://other.invalid/DC=x"] if state["result"] == 10 else None,
            }
            messages = [user] if "sAMAccountName" in search_filter or search_filter == "(objectClass=*)" else []
            messages += [*REFERENCES, done]
            conn.request = {"base": search_base, "filter": search_filter}
            conn.strategy._outstanding = {1: {"attributes": attributes or ["*"], "dereferenceAlias": "always"}}
            conn.strategy._get_response = lambda *a: list(messages) + [RESPONSE_COMPLETE]
            conn._entries = []
            conn.strategy.post_send_search(1)
            return bool(conn.entries)

        conn.search = search
        return conn

    monkeypatch.setattr(ldap, "Connection", factory)
    return state, followed, ldap.parse_ldap_config(values)


async def test_domain_root_login_ignores_successful_continuation_references(directory, async_client, db_session):
    state, followed, _ = directory
    response = await async_client.post("/api/v1/auth/login", json={"username": "fl-alice", "password": "fake-password"})
    assert response.status_code == 200, response.text
    assert response.json()["user"]["auth_source"] == "ldap"
    assert {g["name"] for g in response.json()["user"]["groups"]} == {"Viewers"}
    await db_session.rollback()
    assert await db_session.scalar(select(User).where(User.username == "fl-alice")) is not None
    assert len(state["searches"]) == 1  # AD memberOf requires no group-domain scan
    followed.assert_not_called()


async def test_manual_provision_ignores_successful_continuation_references(directory, async_client):
    _, followed, _ = directory
    response = await async_client.post("/api/v1/auth/ldap/provision", json={"username": "fl-alice"})
    assert response.status_code == 201, response.text
    followed.assert_not_called()


@pytest.mark.parametrize("door", ["save", "draft_test", "saved_test"])
async def test_contradictory_ldaps_tuple_is_422_before_connection(door, async_client, db_session, monkeypatch):
    connection = MagicMock(side_effect=AssertionError("configuration must be rejected before connection"))
    monkeypatch.setattr(ldap, "Connection", connection)
    values = {"ldap_server_url": "LDAP://dc.example.com", "ldap_security": "ldaps"}
    if door == "saved_test":
        db_session.add_all(Settings(key=k, value=v) for k, v in values.items())
        await db_session.commit()
        response = await async_client.post("/api/v1/auth/ldap/test")
    elif door == "draft_test":
        response = await async_client.post("/api/v1/auth/ldap/test", json=values)
    else:
        response = await async_client.patch("/api/v1/settings", json=values)
    assert response.status_code == 422, response.text
    assert "Invalid LDAP destination" in response.text
    connection.assert_not_called()


async def test_legacy_contradictory_ldaps_pair_allows_unrelated_save(async_client, db_session, monkeypatch):
    db_session.add_all(
        Settings(key=k, value=v) for k, v in {"ldap_server_url": "ldap://old.local", "ldap_security": "ldaps"}.items()
    )
    await db_session.commit()
    connection = MagicMock(side_effect=AssertionError("ordinary settings save must not connect"))
    monkeypatch.setattr(ldap, "Connection", connection)
    response = await async_client.patch("/api/v1/settings", json={"currency": "EUR"})
    assert response.status_code == 200, response.text
    assert response.json()["currency"] == "EUR"
    connection.assert_not_called()


async def test_lookup_and_admin_search_ignore_successful_continuation_references(directory):
    _, followed, cfg = directory
    assert ldap.lookup_ldap_user(cfg, "fl-alice").groups == [GROUP_DN]
    assert [u.username for u in ldap.search_ldap_users(cfg, "alice")] == ["fl-alice"]
    followed.assert_not_called()


@pytest.mark.parametrize("result", [0, 4])
async def test_bounded_test_ignores_successful_continuation_references(result, directory, async_client):
    state, followed, _ = directory
    state["result"] = result
    response = await async_client.post("/api/v1/auth/ldap/test")
    assert response.status_code == 200 and response.json()["success"], response.text
    followed.assert_not_called()


@pytest.mark.parametrize("failure", ["operation_referral", "group_continuation"])
async def test_operation_referral_and_incomplete_groups_still_refuse_without_provision(
    failure, directory, async_client, db_session
):
    state, followed, _ = directory
    if failure == "operation_referral":
        state["result"] = 10
    else:
        state["with_groups"] = False
    response = await async_client.post("/api/v1/auth/login", json={"username": "fl-alice", "password": "fake-password"})
    assert response.status_code == 503, response.text
    await db_session.rollback()
    assert await db_session.scalar(select(User).where(User.username == "fl-alice")) is None
    followed.assert_not_called()


@pytest.mark.parametrize("door", ["lookup", "search", "test"])
async def test_operation_referral_stays_refused_on_every_service_search(door, directory):
    state, followed, cfg = directory
    state["result"] = 10
    if door == "test":
        success, message = ldap.test_ldap_connection(cfg)
        assert not success and message == ldap.LDAP_ERROR_MESSAGES["search_failed"]
    else:
        with pytest.raises(ldap.LDAPReferralError):
            (ldap.lookup_ldap_user(cfg, "fl-alice") if door == "lookup" else ldap.search_ldap_users(cfg, "alice"))
    followed.assert_not_called()
