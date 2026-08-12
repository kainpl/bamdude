"""LDAP Distinguished Names must not reach a support bundle (upstream #2681).

A DN's leaf ``CN=`` is the user's real name — PII on par with the email address
this sanitizer already redacts, and it travelled into a file users email to us.

Two halves, and both are needed. Keeping the DN out of the log at the source
handles the one line we control; the sanitizer handles the ones we do not (ldap3
exception strings, group DNs). Neither alone is sufficient.

This is also the one redaction that cannot be value-driven: every other
substitution here works from a table of known values pulled out of the database,
and a DN is per-user and arrives from the directory. So it is matched by shape,
which makes both directions worth pinning — that it catches DNs, and that it
leaves ordinary BamDude log lines alone.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app.api.routes.support import _sanitize_log_content
from backend.app.services.log_reader import sanitize_log_content

REPORTED_LINE = (
    "2026-07-27 09:14:02,113 INFO [backend.app.services.ldap_service] "
    "LDAP authentication successful for user: jschmoe "
    "(DN: CN=Joe Schmoe,CN=Users,DC=ad,DC=example,DC=com, groups: 3)"
)


class TestDnIsRedacted:
    def test_the_reported_line_loses_the_name(self) -> None:
        out = sanitize_log_content(REPORTED_LINE)
        assert "Joe Schmoe" not in out
        assert "[DN]" in out
        # The parts that make the line useful for support survive.
        assert "authentication successful" in out
        assert "jschmoe" in out

    def test_dn_inside_an_ldap3_exception_string(self) -> None:
        """The source-side fix cannot reach these — the library builds them."""
        line = (
            "LDAP bind failed for user jschmoe: invalidCredentials - "
            "80090308: LdapErr: DSID-0C09044E, data 52e, "
            "for CN=Joe Schmoe,OU=Staff,DC=ad,DC=example,DC=com"
        )
        out = sanitize_log_content(line)
        assert "Joe Schmoe" not in out
        assert "[DN]" in out

    def test_lowercase_dn_is_matched_too(self) -> None:
        out = sanitize_log_content("bind_dn=cn=admin,dc=example,dc=com configured")
        assert "admin" not in out

    def test_group_dn_does_not_swallow_what_follows_it(self) -> None:
        """RFC 4514 requires ``<>;+`` escaped inside a DN value, so an unescaped
        one ends the DN — which is what stops the last, comma-unbounded component
        running to the end of the line.

        It stops at the ``>``, not before the ``-``, so a ``-> Name`` suffix comes
        out as ``> Name``. Cosmetic and deliberate: the mapped-to group is what a
        support reader needs and it survives; tightening the value class further
        would start splitting DNs that legitimately contain a hyphen.
        """
        line = "LDAP group mapping: CN=3D Printing,OU=Groups,DC=ad,DC=example,DC=com -> Operators"
        out = sanitize_log_content(line)
        assert "3D Printing" not in out
        assert out == "LDAP group mapping: [DN]> Operators", out


class TestOrdinaryLinesAreLeftAlone:
    """Over-redaction is the failure mode this pattern trades against: a bundle
    redacted into uselessness helps nobody either. Two RDN components minimum is
    what buys that, so these are the lines it must not touch."""

    def test_a_single_key_value_is_not_a_dn(self) -> None:
        line = "Printer 3: not idle - state=RUNNING"
        assert sanitize_log_content(line) == line

    def test_real_bamdude_log_lines_are_never_mistaken_for_a_dn(self) -> None:
        # Drawn from actual format strings in the codebase — the shapes most
        # likely to look DN-ish to a comma-joined key=value pattern. Asserted on
        # ``[DN]`` rather than on equality because some of these legitimately
        # trip the older serial / IP rules, which is not what is under test here.
        lines = [
            "Queue: printer 3 not available - connected=True, state=FINISH, awaiting_plate_clear=False",
            "BamDude starting - debug=False, log_level=INFO",
            "Created custom macro: Purge (event=print_start, type=mqtt_action)",
            "Sent SSDP NOTIFY for 01P00A123456789 (Location=http://h:8000, USN=uuid:x, bind=0.0.0.0)",
            "Spool assign: local preset 4, material='PLA', tray_info_idx='GFA01'",
            "  - extruder_id=0, name='PAHT-CF', k_value=0.042",
            "Applied K-profile (k=0.042, name='PAHT-CF') for spool 9 on printer 2 AMS0-T1",
            "Zigbee sensor 0x00124b00 reported temperature=21.5",
        ]
        for line in lines:
            assert "[DN]" not in sanitize_log_content(line), line

    def test_a_line_with_no_ldap_shape_is_returned_verbatim(self) -> None:
        line = "Created custom macro: Purge (event=print_start, type=mqtt_action)"
        assert sanitize_log_content(line) == line

    def test_an_accept_language_header_is_not_a_dn(self) -> None:
        line = 'Request headers: Accept-Language="en-US,en;q=0.9"'
        assert sanitize_log_content(line) == line


class TestBothSanitizersRedact:
    """Ours, beyond upstream.

    ``routes/support.py`` carried a byte-identical copy of this function, and the
    support bundle — the artefact #2681 is actually about — went through *that*
    one. Porting the pattern into ``log_reader`` alone would have left the bundle
    leaking the DN while every test on the other copy passed.
    """

    def test_the_support_bundle_sanitizer_redacts_dns_as_well(self) -> None:
        out = _sanitize_log_content(REPORTED_LINE)
        assert "Joe Schmoe" not in out
        assert "[DN]" in out

    def test_the_two_are_one_function(self) -> None:
        content = "CN=Joe Schmoe,DC=example,DC=com and 192.168.1.5 and joe@example.com"
        assert _sanitize_log_content(content) == sanitize_log_content(content)

    def test_the_alias_still_applies_the_known_value_table(self) -> None:
        """Delegation must not have dropped the argument the bundle relies on."""
        out = _sanitize_log_content("printer 'Barn A1' went offline", {"Barn A1": "[PRINTER]"})
        assert "Barn A1" not in out
        assert "[PRINTER]" in out


class TestSourceSideHygiene:
    """The primary half: never write it in the first place."""

    def test_successful_auth_logs_no_dn(self, caplog) -> None:
        from backend.app.services import ldap_service

        config = ldap_service.LDAPConfig(
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

        entry = MagicMock()
        entry.entry_dn = "CN=Joe Schmoe,CN=Users,DC=ad,DC=example,DC=com"
        service_conn = MagicMock()
        service_conn.entries = [entry]

        info = SimpleNamespace(username="jschmoe", groups=["Operators", "Staff", "All"])

        with (
            patch.object(ldap_service, "_create_server"),
            patch.object(ldap_service, "_open_service_connection", return_value=service_conn),
            patch.object(ldap_service, "Connection", return_value=MagicMock()),
            patch.object(ldap_service, "_extract_user_info", return_value=info),
            caplog.at_level(logging.INFO, logger="backend.app.services.ldap_service"),
        ):
            assert ldap_service.authenticate_ldap_user(config, "jschmoe", "pw") is info

        text = caplog.text
        assert "authentication successful" in text
        assert "jschmoe" in text
        assert "groups: 3" in text
        assert "Joe Schmoe" not in text, "the user's real name must not reach the log file"
        assert "CN=" not in text
