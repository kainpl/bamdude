"""FL2/FL3 transport proof against real ldap3 and bounded loopback sockets."""

import socket
import ssl
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ldap3.core.exceptions import LDAPCertificateError, LDAPSocketOpenError, communication_exception_factory

from backend.app.services import ldap_service as ldap
from backend.tests.unit.services.test_g01_f_ldap_directory import config


@pytest.mark.parametrize(
    "scheme,expected", [("ldap", "configuration_failed"), ("LDAP", "configuration_failed"), ("ldaps", "tls_failed")]
)
def test_ldaps_mode_never_sends_plaintext_bind_to_loopback(scheme, expected):
    secret = b"FL2-FAKE-BIND-PASSWORD"
    received = bytearray()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]

        def accept():
            try:
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(2)
                    received.extend(peer.recv(4096))
            except OSError:
                pass

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        success, message = ldap.test_ldap_connection(
            config(server_url=f"{scheme}://127.0.0.1:{port}", security="ldaps", bind_password=secret.decode())
        )
        thread.join(3)
        assert not thread.is_alive()
    assert not success and message == ldap.LDAP_ERROR_MESSAGES[expected]
    assert secret not in received
    if scheme == "ldaps":
        assert received[:1] == b"\x16"  # actual TLS ClientHello, then peer closes
    else:
        assert not received  # invalid tuple refused before connect


def test_unreachable_ldaps_is_connection_failure():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))  # reserved, intentionally not listening
        port = reserved.getsockname()[1]
        success, message = ldap.test_ldap_connection(config(server_url=f"ldaps://127.0.0.1:{port}", security="ldaps"))
    assert not success
    assert message == ldap.LDAP_ERROR_MESSAGES["connection_failed"]


@pytest.mark.parametrize("failure", [ssl.SSLCertVerificationError("CANARY"), LDAPCertificateError("CANARY")])
def test_typed_ldaps_certificate_failure_keeps_safe_tls_category(failure, monkeypatch):
    conn = MagicMock()
    # Match installed ldap3's typed communication wrapper without exposing text.
    conn.open.side_effect = communication_exception_factory(LDAPSocketOpenError, failure)("CANARY")
    monkeypatch.setattr(ldap, "Connection", lambda *a, **kw: conn)
    with pytest.raises(ldap.LDAPOperationError) as exc:
        ldap._open_service_connection(config(security="ldaps"), SimpleNamespace(ssl=True))
    assert exc.value.code == "tls_failed" and "CANARY" not in str(exc.value)
    conn.bind.assert_not_called()


@pytest.mark.parametrize(
    "url,security,ssl_enabled,port",
    [
        ("dc.example.com", "ldaps", True, 636),
        ("LDAPS://dc.example.com", "ldaps", True, 636),
        ("ldaps://dc.example.com", "starttls", True, 636),
        ("ldap://dc.example.com", "starttls", False, 389),
        ("ldap://dc.example.com", "none", False, 389),
    ],
)
def test_supported_transport_tuples_keep_ldap3_semantics(url, security, ssl_enabled, port):
    server = ldap._create_server(config(server_url=url, security=security))
    assert server.ssl is ssl_enabled and server.port == port


@pytest.mark.parametrize(
    "last_error,expected",
    [
        ("socket ssl wrapping error: CANARY", "tls_failed"),
        ("socket connection error while opening: certificate TLS CANARY", "connection_failed"),
    ],
)
def test_ldap3_stage_signal_survives_cleanup_without_using_peer_prose(last_error, expected, monkeypatch):
    conn = MagicMock()

    def open_socket(**kwargs):
        conn.last_error = last_error
        raise LDAPSocketOpenError("CANARY")

    def unbind():
        conn.last_error = None  # actual ldap3 Connection.unbind behavior

    conn.open.side_effect = open_socket
    conn.unbind.side_effect = unbind
    monkeypatch.setattr(ldap, "Connection", lambda *a, **kw: conn)
    with pytest.raises(ldap.LDAPOperationError) as exc:
        ldap._open_service_connection(config(security="ldaps"), SimpleNamespace(ssl=True))
    assert conn.last_error is None
    assert exc.value.code == expected and "CANARY" not in str(exc.value)
    conn.bind.assert_not_called()
