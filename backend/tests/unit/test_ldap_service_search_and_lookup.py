"""Unit tests for the new LDAP-service helpers added in upstream Bambuddy
#1298 / commit d6364646:

- ``_open_service_connection`` with ``check_names`` toggle.
- ``_pick_canonical_username`` precedence (sAMAccountName → uid → fallback).
- ``lookup_ldap_user`` (no password, service-bind only).
- ``search_ldap_users`` fuzzy directory search.

The existing ``authenticate_ldap_user`` regression suite still covers the
full login flow — these tests only pin the new code paths.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.ldap_service import (
    LDAPConfig,
    LDAPSearchResult,
    _pick_canonical_username,
    lookup_ldap_user,
    search_ldap_users,
)


def _config(**overrides):
    base = LDAPConfig(
        server_url="ldap://example.test",
        bind_dn="cn=admin,dc=example,dc=com",
        bind_password="secret",
        search_base="dc=example,dc=com",
        user_filter="(uid={username})",
        security="none",
        group_mapping={},
        auto_provision=False,
        ca_cert_path="",
        default_group="",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestPickCanonicalUsername:
    def test_prefers_sam_account_name(self):
        entry = SimpleNamespace(sAMAccountName="alice", uid="alice@old", cn="Alice")
        assert _pick_canonical_username(entry, "fallback") == "alice"

    def test_falls_back_to_uid(self):
        entry = SimpleNamespace(sAMAccountName=None, uid="bob")
        assert _pick_canonical_username(entry, "fallback") == "bob"

    def test_uses_supplied_fallback_when_no_canonical_attr(self):
        entry = SimpleNamespace()
        assert _pick_canonical_username(entry, "fallback-name") == "fallback-name"

    def test_empty_attribute_treated_as_missing(self):
        entry = SimpleNamespace(sAMAccountName="", uid="bob")
        assert _pick_canonical_username(entry, "fallback") == "bob"


class TestSearchLdapUsersMinLength:
    """A typed ``*`` or single character must NOT enumerate the whole
    directory. The function short-circuits before any LDAP call."""

    def test_empty_query_returns_empty(self):
        result = search_ldap_users(_config(), "")
        assert result == []

    def test_whitespace_only_query_returns_empty(self):
        result = search_ldap_users(_config(), "   ")
        assert result == []

    def test_single_character_returns_empty(self):
        result = search_ldap_users(_config(), "a")
        assert result == []


class TestSearchLdapUsersResultMapping:
    """``search_ldap_users`` should return ``LDAPSearchResult`` dataclasses
    with the right shape for each ldap3 entry."""

    @pytest.mark.asyncio
    async def test_maps_entries_to_search_results(self):
        entry_alice = SimpleNamespace(
            entry_dn="uid=alice,dc=example,dc=com",
            sAMAccountName="alice",
            uid=None,
            mail="alice@example.com",
            displayName="Alice Smith",
        )
        entry_bob = SimpleNamespace(
            entry_dn="uid=bob,dc=example,dc=com",
            sAMAccountName=None,
            uid="bob",
            mail=None,
            displayName=None,
        )
        mock_conn = MagicMock()
        mock_conn.entries = [entry_alice, entry_bob]
        mock_conn.search = MagicMock()
        mock_conn.unbind = MagicMock()

        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch("backend.app.services.ldap_service._open_service_connection", return_value=mock_conn),
        ):
            results = search_ldap_users(_config(), "ali", limit=10)

        # Each entry mapped to a dataclass row
        assert len(results) == 2
        assert all(isinstance(r, LDAPSearchResult) for r in results)
        # Alice: sAMAccountName picked over uid (which is None)
        assert results[0].username == "alice"
        assert results[0].email == "alice@example.com"
        assert results[0].display_name == "Alice Smith"
        assert results[0].dn == "uid=alice,dc=example,dc=com"
        # Bob: uid picked because sAMAccountName is None; email/display_name None
        assert results[1].username == "bob"
        assert results[1].email is None
        assert results[1].display_name is None

    @pytest.mark.asyncio
    async def test_empty_search_returns_empty_list(self):
        mock_conn = MagicMock()
        mock_conn.entries = []
        mock_conn.search = MagicMock()
        mock_conn.unbind = MagicMock()

        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch("backend.app.services.ldap_service._open_service_connection", return_value=mock_conn),
        ):
            results = search_ldap_users(_config(), "nobody")

        assert results == []

    @pytest.mark.asyncio
    async def test_bind_failure_raises(self):
        """Service-bind failure must propagate so the route can surface a 503."""
        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch(
                "backend.app.services.ldap_service._open_service_connection",
                side_effect=RuntimeError("LDAP bind failed: invalid creds"),
            ),
            pytest.raises(RuntimeError, match="LDAP bind failed"),
        ):
            search_ldap_users(_config(), "alice")


class TestSearchLdapUsersUsesUncheckedNames:
    """The search path MUST open the service connection with
    ``check_names=False`` so ldap3 doesn't reject the cross-schema OR
    filter (sAMAccountName / displayName are AD-only)."""

    @pytest.mark.asyncio
    async def test_check_names_disabled_for_search(self):
        captured = {}

        def fake_open(config, server, *, check_names=True):
            captured["check_names"] = check_names
            conn = MagicMock()
            conn.entries = []
            conn.search = MagicMock()
            conn.unbind = MagicMock()
            return conn

        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch("backend.app.services.ldap_service._open_service_connection", side_effect=fake_open),
        ):
            search_ldap_users(_config(), "alice")

        assert captured.get("check_names") is False, (
            "search path must pass check_names=False to tolerate OpenLDAP schemas"
        )


class TestLookupLdapUser:
    """``lookup_ldap_user`` skips password verification and uses the same
    ``user_filter`` template that the login path uses."""

    @pytest.mark.asyncio
    async def test_returns_user_info_on_match(self):
        entry = SimpleNamespace(
            entry_dn="uid=alice,dc=example,dc=com",
            sAMAccountName=None,
            uid="alice",
            mail="alice@example.com",
            displayName="Alice Smith",
            memberOf=["cn=admins,dc=example,dc=com"],
            gidNumber=None,
        )
        mock_conn = MagicMock()
        mock_conn.entries = [entry]
        mock_conn.search = MagicMock()
        mock_conn.unbind = MagicMock()

        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch("backend.app.services.ldap_service._open_service_connection", return_value=mock_conn),
        ):
            info = lookup_ldap_user(_config(), "alice")

        assert info is not None
        assert info.username == "alice"
        assert info.email == "alice@example.com"
        assert info.display_name == "Alice Smith"
        assert "cn=admins,dc=example,dc=com" in info.groups

    @pytest.mark.asyncio
    async def test_returns_none_when_user_not_found(self):
        mock_conn = MagicMock()
        mock_conn.entries = []
        mock_conn.search = MagicMock()
        mock_conn.unbind = MagicMock()

        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch("backend.app.services.ldap_service._open_service_connection", return_value=mock_conn),
        ):
            info = lookup_ldap_user(_config(), "ghost")

        assert info is None

    @pytest.mark.asyncio
    async def test_bind_failure_raises(self):
        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch(
                "backend.app.services.ldap_service._open_service_connection",
                side_effect=RuntimeError("bind failed"),
            ),
            pytest.raises(RuntimeError, match="bind failed"),
        ):
            lookup_ldap_user(_config(), "alice")

    @pytest.mark.asyncio
    async def test_uses_default_check_names_for_lookup(self):
        """Lookup goes through the standard schema-checked bind so typos in
        user_filter still fail loudly."""
        captured = {}

        def fake_open(config, server, *, check_names=True):
            captured["check_names"] = check_names
            conn = MagicMock()
            conn.entries = []
            conn.search = MagicMock()
            conn.unbind = MagicMock()
            return conn

        with (
            patch("backend.app.services.ldap_service._create_server"),
            patch("backend.app.services.ldap_service._open_service_connection", side_effect=fake_open),
        ):
            lookup_ldap_user(_config(), "alice")

        # Default kwarg used → True
        assert captured.get("check_names", True) is True
