"""F interoperability and completeness against real local ldap3 strategies."""

import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from ldap3 import MOCK_SYNC, NONE, OFFLINE_AD_2012_R2, OFFLINE_SLAPD_2_4, Connection, Server

from backend.app.services import ldap_service as ldap


def config(**changes):
    values = {
        "server_url": "ldap://directory.test",
        "bind_dn": "cn=service,dc=example,dc=com",
        "bind_password": "secret",
        "search_base": "ou=people,dc=example,dc=com",
        "user_filter": "(uid={username})",
        "security": "starttls",
        "group_mapping": {},
        "auto_provision": True,
        "ca_cert_path": "",
        "default_group": "Administrators",
    }
    return ldap.LDAPConfig(**{**values, **changes})


def directory(monkeypatch, schema=NONE):
    server = Server("directory.test", get_info=schema)
    conn = Connection(server, client_strategy=MOCK_SYNC, raise_exceptions=True)
    conn.bind()
    if server.info:
        server.info.naming_contexts = ["dc=example,dc=com"]
    monkeypatch.setattr(ldap, "_create_server", lambda cfg: server)
    monkeypatch.setattr(ldap, "_open_service_connection", lambda *args, **kwargs: conn)
    conn.search = MagicMock(wraps=conn.search)
    return conn


def test_ad_memberof_explicit_and_no_extra_domain_lookup(monkeypatch):
    conn = directory(monkeypatch, OFFLINE_AD_2012_R2)
    dn = "cn=Alice,ou=people,dc=example,dc=com"
    group = "cn=Operators,ou=groups,dc=example,dc=com"
    conn.strategy.add_entry(dn, {"objectClass": ["person", "user"], "sAMAccountName": "alice", "memberOf": [group]})
    info = ldap.lookup_ldap_user(config(user_filter="(sAMAccountName={username})"), "alice")
    assert info.groups == [group]
    assert len(conn.search.call_args_list) == 1
    assert "memberOf" in conn.search.call_args.kwargs["attributes"]


def test_openldap_member_unique_member_and_posix_use_shared_naming_context(monkeypatch):
    conn = directory(monkeypatch, OFFLINE_SLAPD_2_4)
    dn = "uid=alice,ou=people,dc=example,dc=com"
    conn.strategy.add_entry(
        dn,
        {
            "objectClass": ["inetOrgPerson", "posixAccount"],
            "uid": "alice",
            "cn": "Alice",
            "sn": "A",
            "gidNumber": "1000",
        },
    )
    expected = [
        "cn=Members,ou=groups,dc=example,dc=com",
        "cn=Unique,ou=groups,dc=example,dc=com",
        "cn=Unix,ou=groups,dc=example,dc=com",
    ]
    conn.strategy.add_entry(expected[0], {"objectClass": "groupOfNames", "member": dn})
    conn.strategy.add_entry(expected[1], {"objectClass": "groupOfUniqueNames", "uniqueMember": dn})
    conn.strategy.add_entry(expected[2], {"objectClass": "posixGroup", "gidNumber": "1000", "memberUid": "alice"})
    info = ldap.lookup_ldap_user(config(), "alice")
    assert set(info.groups) == set(expected)
    assert len(info.groups) == 3
    assert all(c.kwargs["search_base"] == "dc=example,dc=com" for c in conn.search.call_args_list[1:])


def test_unknown_schema_and_missing_user_objectclass_still_resolve_member_dn(monkeypatch):
    conn = directory(monkeypatch)
    dn = "uid=alice,ou=people,dc=example,dc=com"
    group = "cn=Members,ou=groups,dc=example,dc=com"
    conn.strategy.add_entry(dn, {"uid": "alice"})
    conn.strategy.add_entry(group, {"objectClass": "groupOfNames", "member": dn})
    assert ldap.lookup_ldap_user(config(), "alice").groups == [group]


@pytest.mark.parametrize("result_code", [3, 4, 10, 50])
def test_partial_group_results_never_become_complete_empty_or_privileged_default(result_code):
    conn = SimpleNamespace(server=SimpleNamespace(schema=None, info=None), entries=[], result={"result": result_code})

    def search(**kwargs):
        conn.entries = []
        conn.result = {"result": result_code}
        return False

    conn.search = search
    user = SimpleNamespace(entry_dn="uid=alice,ou=people,dc=example,dc=com", uid="alice")
    expected = ldap.LDAPReferralError if result_code == 10 else ldap.LDAPGroupLookupError
    with pytest.raises(expected):
        ldap._extract_user_info(conn, config(), user, "alice")


def test_result_zero_false_is_valid_empty():
    conn = SimpleNamespace(server=SimpleNamespace(schema=None, info=None), entries=[], result={"result": 0})
    conn.search = lambda **kwargs: False
    user = SimpleNamespace(entry_dn="uid=alice,ou=people,dc=example,dc=com", uid="alice")
    assert ldap._extract_user_info(conn, config(), user, "alice").groups == []


def test_starttls_schema_is_read_only_after_authenticated_bind(monkeypatch):
    conn = MagicMock()
    conn.bind.return_value = True
    conn.start_tls.return_value = True
    monkeypatch.setattr(ldap, "Connection", lambda *args, **kwargs: conn)
    ldap._open_service_connection(config(), SimpleNamespace(ssl=False))
    assert conn.mock_calls == [
        call.open(read_server_info=False),
        call.start_tls(read_server_info=False),
        call.bind(read_server_info=False),
        call.refresh_server_info(),
    ]


@pytest.mark.parametrize("failure", [False, ssl.SSLCertVerificationError("certificate CANARY_SECRET")])
def test_starttls_failure_never_binds_plaintext_and_is_safe(monkeypatch, failure):
    conn = MagicMock()
    conn.result = {"result": 53} if failure is False else {"result": 0}
    if failure is False:
        conn.start_tls.return_value = False
    else:
        conn.start_tls.side_effect = failure
    monkeypatch.setattr(ldap, "Connection", lambda *args, **kwargs: conn)
    with pytest.raises(Exception) as error:
        ldap._open_service_connection(config(), SimpleNamespace(ssl=False))
    assert "CANARY" not in str(error.value)
    assert getattr(error.value, "code", None) == ("starttls_refused" if failure is False else "tls_failed")
    conn.bind.assert_not_called()
    conn.unbind.assert_called_once()


def test_mapping_casefold_deduplicates_equivalent_dn_and_same_destination_group():
    assert ldap.resolve_group_mapping(
        ["cn=STRASSE,dc=x", "cn=Other,dc=x"], {"cn=Straße,dc=x": "Operators", "cn=Other,dc=x": "Operators"}
    ) == ["Operators"]


def test_filter_escaping_for_user_dn_username_and_primary_gid_with_real_strategy(monkeypatch):
    conn = directory(monkeypatch)
    username = "ali*ce(1)"
    dn = f"uid={username},ou=people,dc=example,dc=com"
    gid = "10*(0)"
    conn.strategy.add_entry(dn, {"uid": username, "gidNumber": gid})
    expected = ["cn=Direct,ou=groups,dc=example,dc=com", "cn=Unix,ou=groups,dc=example,dc=com"]
    conn.strategy.add_entry(expected[0], {"objectClass": "groupOfNames", "member": dn})
    conn.strategy.add_entry(expected[1], {"objectClass": "posixGroup", "memberUid": username, "gidNumber": gid})
    conn.strategy.add_entry(
        "cn=Unrelated,ou=groups,dc=example,dc=com",
        {"objectClass": "posixGroup", "memberUid": "alixce1", "gidNumber": "100"},
    )
    assert set(ldap.lookup_ldap_user(config(), username).groups) == set(expected)


def test_empty_schema_metadata_is_unknown_and_dn_casefold_dedup_stays_complete():
    conn = SimpleNamespace(
        server=SimpleNamespace(schema=SimpleNamespace(attribute_types={}, object_classes={}), info=None),
        response=[],
        entries=[],
        result={"result": 0},
    )

    def search(**kwargs):
        conn.entries = [SimpleNamespace(entry_dn="cn=STRASSE,dc=example,dc=com")]
        return True

    conn.search = search
    user = SimpleNamespace(
        entry_dn="uid=alice,ou=people,dc=example,dc=com", uid="alice", memberOf=["cn=Straße,dc=example,dc=com"]
    )
    assert ldap._extract_user_info(conn, config(), user, "alice").groups == ["cn=Straße,dc=example,dc=com"]


def test_context_selects_longest_advertised_suffix_and_non_dc_falls_back():
    conn = SimpleNamespace(
        server=SimpleNamespace(
            info=SimpleNamespace(naming_contexts=["dc=example,dc=com", "ou=tenant,dc=example,dc=com"])
        )
    )
    assert ldap._group_search_base(conn, config(), "uid=a,ou=tenant,dc=example,dc=com") == "ou=tenant,dc=example,dc=com"
    conn.server.info = None
    assert ldap._group_search_base(conn, config(search_base="o=org"), "uid=a,ou=people,o=org") == "o=org"


def test_partial_referral_with_success_result_is_still_incomplete():
    conn = SimpleNamespace(result={"result": 0}, response=[{"type": "searchResRef"}], entries=[])
    conn.search = lambda **kwargs: False
    with pytest.raises(ldap.LDAPReferralError):
        ldap._search_entries(conn, groups=True, search_base="dc=x", search_filter="(objectClass=*)")


def test_authenticated_schema_refusal_leaves_a_usable_unknown_schema(monkeypatch):
    from ldap3.core.exceptions import LDAPInsufficientAccessRightsResult

    conn = MagicMock()
    conn.bind.return_value = conn.start_tls.return_value = True
    conn.refresh_server_info.side_effect = LDAPInsufficientAccessRightsResult("CANARY_DRIVER")
    monkeypatch.setattr(ldap, "Connection", lambda *args, **kwargs: conn)
    assert ldap._open_service_connection(config(), SimpleNamespace(ssl=False)) is conn
    conn.unbind.assert_not_called()


def test_user_password_verification_uses_tls_without_refreshing_service_schema(monkeypatch):
    conn = MagicMock()
    conn.bind.return_value = conn.start_tls.return_value = True
    monkeypatch.setattr(ldap, "Connection", lambda *args, **kwargs: conn)
    ldap._open_bound_connection(
        config(), SimpleNamespace(ssl=False), "uid=alice,dc=x", "password", refresh_schema=False
    )
    conn.start_tls.assert_called_once_with(read_server_info=False)
    conn.bind.assert_called_once_with(read_server_info=False)
    conn.refresh_server_info.assert_not_called()


def owned_sync_connection(monkeypatch):
    """Use the production constructor and real SyncStrategy, replacing only I/O."""

    def create(*args, **kwargs):
        conn = Connection(*args, **kwargs)
        conn.open = MagicMock()
        conn.start_tls = MagicMock(return_value=True)
        conn.bind = MagicMock(return_value=True)
        conn.refresh_server_info = MagicMock()
        return conn

    monkeypatch.setattr(ldap, "Connection", create)
    return ldap._open_service_connection(config(), Server("directory.invalid", get_info=OFFLINE_AD_2012_R2))


@pytest.mark.parametrize("ranged", [False, True])
def test_real_sync_strategy_cannot_hide_incomplete_memberof_range(monkeypatch, ranged):
    from ldap3.strategy.base import RESPONSE_COMPLETE

    conn = owned_sync_connection(monkeypatch)
    group = "cn=Unmapped,ou=groups,dc=example,dc=com"
    attr = "memberOf;range=0-1499" if ranged else "memberOf"
    conn.request = {"base": "dc=example,dc=com", "filter": "(sAMAccountName=alice)"}
    conn.strategy._outstanding = {1: {"attributes": ["*", "memberOf"], "dereferenceAlias": "always"}}
    initial = {
        "type": "searchResEntry",
        "dn": "cn=Alice,ou=people,dc=example,dc=com",
        "attributes": {"sAMAccountName": ["alice"], attr: [group]},
        "raw_attributes": {"sAMAccountName": [b"alice"], attr: [group.encode()]},
    }
    success = {
        "type": "searchResDone",
        "result": 0,
        "description": "success",
        "dn": "",
        "message": "",
        "referrals": None,
    }
    conn.strategy._get_response = lambda *args: [initial, success, RESPONSE_COMPLETE]
    continuations = []

    def search(**kwargs):
        if hasattr(conn.strategy, "_auto_range_searching"):
            continuations.append(kwargs)
            conn.result, conn.response = {"result": 4}, []
            return False
        conn.strategy.post_send_search(1)
        return True

    conn.search = search
    entries = ldap._search_entries(conn, search_base="dc=example,dc=com", search_filter="(sAMAccountName=alice)")
    if ranged:
        with pytest.raises(ldap.LDAPGroupLookupError):
            ldap._extract_user_info(conn, config(), entries[0], "alice")
    else:
        assert ldap._extract_user_info(conn, config(), entries[0], "alice").groups == [group]
    assert continuations == []
    assert conn.auto_range is False


@pytest.mark.parametrize("groups", [False, True])
def test_real_sync_strategy_referral_never_opens_other_destination(monkeypatch, groups):
    from ldap3.strategy.base import RESPONSE_COMPLETE

    conn = owned_sync_connection(monkeypatch)
    conn.request = {"base": "dc=example,dc=com", "filter": "(uid=alice)"}
    conn.strategy._outstanding = {1: {"attributes": ["*"], "dereferenceAlias": "always"}}
    referral = {
        "type": "searchResDone",
        "result": 10,
        "description": "referral",
        "dn": "",
        "message": "",
        "referrals": ["ldap://other.invalid/dc=target"],
    }
    conn.strategy._get_response = lambda *args: [referral, RESPONSE_COMPLETE]
    conn.strategy.do_operation_on_referral = MagicMock(return_value=(None, None))
    conn.search = lambda **kwargs: conn.strategy.post_send_search(1)
    with pytest.raises(ldap.LDAPOperationError):
        ldap._search_entries(conn, groups=groups, search_base="dc=example,dc=com", search_filter="(uid=alice)")
    conn.strategy.do_operation_on_referral.assert_not_called()
    assert conn.auto_referrals is False
