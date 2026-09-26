"""A printer that answers the FTPS port without TLS is asked what it said (audit D5, upstream cc39acfc + 10f0900f, #2780).

``[SSL: WRONG_VERSION_NUMBER]`` means the printer's first bytes on port 990 were
not a TLS record — reproducible without a printer: a cleartext "421" banner
gives exactly that error, and a TLS version mismatch gives a different one
(``TLSV1_ALERT_PROTOCOL_VERSION``). So "update the firmware, check the firewall"
was the wrong advice, and "the file service wedged, restart" is a guess. What
names the fault is WHICH cleartext the printer sent — and OpenSSL has eaten
those bytes by the time the error surfaces. So on that error, and only that
one, the client opens one plain connection and reads them, and the log carries
the printer's own words. At most once per window per printer, after the failed
socket is closed: the leading theory is a printer out of connection slots, and
a probe must not make that worse.

Upstream pauses all FTP to the printer after such a failure (a cool-off) and
probes once per pause; we have no cool-off (our FTP is serialised per printer
instead), so the probe keeps its own per-printer timestamp.
"""

from __future__ import annotations

import socket
import ssl
import threading
from unittest.mock import MagicMock, patch

import pytest

from backend.app.core.config import settings
from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import BambuFTPClient, _read_cleartext_reply
from backend.app.services.log_health import scan_logs

FTP_LOGGER = "backend.app.services.bambu_ftp"


@pytest.fixture(autouse=True)
def _fresh_probe_window():
    BambuFTPClient._cleartext_probed_at.clear()
    yield
    BambuFTPClient._cleartext_probed_at.clear()


def _wrong_version() -> ssl.SSLError:
    err = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1032)")
    err.reason = "WRONG_VERSION_NUMBER"
    return err


def _failing_tls(error: BaseException):
    """An ImplicitFTP_TLS whose connect raises ``error``; records close()."""
    ftp = MagicMock()
    ftp.connect.side_effect = error
    return ftp


# -- the socket of a failed connect is closed (10f0900f) ----------------------


@pytest.mark.parametrize(
    "error",
    [_wrong_version(), TimeoutError("timed out"), OSError("refused")],
    ids=["tls", "timeout", "network"],
)
def test_a_failed_connect_closes_its_socket(error):
    ftp = _failing_tls(error)
    with (
        patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=ftp),
        patch.object(bambu_ftp, "_read_cleartext_reply", return_value=None),
    ):
        assert BambuFTPClient("10.0.0.9", "12345678").connect() is False
    ftp.close.assert_called_once()


def test_a_failed_quit_still_closes_the_socket():
    client = BambuFTPClient("10.0.0.9", "12345678")
    ftp = MagicMock()
    ftp.quit.side_effect = OSError("broken pipe")
    client._ftp = ftp
    client.disconnect()
    ftp.close.assert_called_once()
    assert client._ftp is None


# -- the probe -----------------------------------------------------------------


def test_the_printers_cleartext_reply_is_logged(caplog):
    ftp = _failing_tls(_wrong_version())
    order: list[str] = []
    ftp.close.side_effect = lambda: order.append("closed")

    def probe(ip, port):
        order.append("probed")
        return "421 Too many connections"

    with (
        patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=ftp),
        patch.object(bambu_ftp, "_read_cleartext_reply", side_effect=probe),
        caplog.at_level("WARNING", logger=FTP_LOGGER),
    ):
        BambuFTPClient("10.0.0.9", "12345678").connect()

    assert order == ["closed", "probed"], "the dead socket goes before a second connection opens"
    assert any("421 Too many connections" in r.getMessage() for r in caplog.records)


def test_the_probe_runs_once_per_window_per_printer():
    probe = MagicMock(return_value="421 Too many connections")
    with (
        patch.object(bambu_ftp, "ImplicitFTP_TLS", side_effect=lambda **_: _failing_tls(_wrong_version())),
        patch.object(bambu_ftp, "_read_cleartext_reply", probe),
    ):
        BambuFTPClient("10.0.0.9", "12345678").connect()
        BambuFTPClient("10.0.0.9", "12345678").connect()
        BambuFTPClient("10.0.0.10", "12345678").connect()

    assert [c.args[0] for c in probe.call_args_list] == ["10.0.0.9", "10.0.0.10"]


def test_only_a_cleartext_answer_is_probed():
    """A protocol-version alert means the peer DID speak TLS — nothing to read."""
    alert = ssl.SSLError(1, "[SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version")
    alert.reason = "TLSV1_ALERT_PROTOCOL_VERSION"
    probe = MagicMock()
    with (
        patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=_failing_tls(alert)),
        patch.object(bambu_ftp, "_read_cleartext_reply", probe),
    ):
        BambuFTPClient("10.0.0.9", "12345678").connect()
    probe.assert_not_called()


def _server(reply: bytes | None):
    """A one-shot TCP server on localhost that sends ``reply`` (or nothing)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        if reply is not None:
            conn.sendall(reply)
        threading.Event().wait(0.5)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_reading_the_reply_off_a_plain_connection():
    port = _server(b"421 Too many connections (2) from this IP\r\n\x00")
    assert _read_cleartext_reply("127.0.0.1", port) == "421 Too many connections (2) from this IP"


def test_a_silent_port_answers_none():
    port = _server(None)
    assert _read_cleartext_reply("127.0.0.1", port) is None


def test_a_refused_port_answers_none():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert _read_cleartext_reply("127.0.0.1", port) is None


# -- the diagnostic tells the two failures apart --------------------------------


def _write(tmp_path, monkeypatch, lines):
    (tmp_path / "bamdude.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "log_dir", tmp_path)


def _line(msg):
    return f"2026-09-26 10:00:00,000 WARNING [{FTP_LOGGER}] {msg}"


def test_a_cleartext_answer_is_its_own_finding(tmp_path, monkeypatch):
    msg = "FTP SSL error connecting to 10.0.0.9: [SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1032)"
    _write(tmp_path, monkeypatch, [_line(msg)] * 3)
    assert [f.signature_id for f in scan_logs().findings] == ["ftp-tls-cleartext"]


def test_any_other_tls_failure_stays_the_generic_one(tmp_path, monkeypatch):
    msg = "FTP SSL error connecting to 10.0.0.9: [SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version"
    _write(tmp_path, monkeypatch, [_line(msg)] * 3)
    assert [f.signature_id for f in scan_logs().findings] == ["ftp-ssl-error"]


# -- the two measurements the diagnostic rests on (pinned, so it stays falsifiable)


def _tls12_only_server(cert: str, key: str, accepts: int):
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    server_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve():
        for _ in range(accepts):
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            try:
                server_ctx.wrap_socket(conn, server_side=True).close()
            except (ssl.SSLError, OSError):
                conn.close()
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


def _handshake(port: int, *, force_tls13: bool) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3 if force_tls13 else ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection(("127.0.0.1", port), 5)
    try:
        ctx.wrap_socket(raw, server_hostname="printer").do_handshake()
    finally:
        raw.close()


def test_a_cleartext_banner_is_what_produces_wrong_version_number():
    port = _server(b"421 Too many connections\r\n")
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), 5)
    try:
        with pytest.raises(ssl.SSLError) as caught:
            ctx.wrap_socket(raw, server_hostname="printer").do_handshake()
    finally:
        raw.close()
    assert caught.value.reason == "WRONG_VERSION_NUMBER"


def test_a_version_mismatch_is_a_different_error_and_no_cap_is_needed(ftp_certs):
    """So capping TLS cannot be the fix for WRONG_VERSION_NUMBER: a real mismatch
    reports a protocol-version alert, and a 1.2-only peer connects uncapped."""
    cert, key = ftp_certs
    port = _tls12_only_server(cert, key, accepts=2)

    with pytest.raises(ssl.SSLError) as caught:
        _handshake(port, force_tls13=True)
    assert caught.value.reason != "WRONG_VERSION_NUMBER"

    _handshake(port, force_tls13=False)  # negotiates 1.2 and connects
