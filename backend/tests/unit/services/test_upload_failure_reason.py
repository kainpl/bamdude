"""A failed upload says why, instead of blaming the SD card (upstream 70ee5346).

Every failed dispatch to a card said "Check if SD card is inserted and properly
formatted (FAT32/exFAT)" — after a refused TLS handshake, a rejected access code
or a timeout alike, none of which ever reached the card. The FTP client already
knew which it was (its connect branches and the 553 / 552 / 550 replies each log
their own line) and returned a bare False.

The reason now travels with the result, in a report the CALLER owns — not a
per-printer record beside the connect failures: a timelapse fetch or a listing
running beside a dispatch would overwrite it and report the wrong cause with
full confidence. The card is named only where the printer itself raised storage.
"""

from __future__ import annotations

import ftplib
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services import bambu_ftp
from backend.app.services.bambu_ftp import (
    BambuFTPClient,
    FtpFailure,
    UploadReport,
    describe_upload_failure,
    upload_file_async,
)


@pytest.fixture(autouse=True)
def _fresh():
    BambuFTPClient._mode_cache.clear()
    BambuFTPClient._last_connect_failure.clear()
    BambuFTPClient._cleartext_probed_at.clear()
    yield
    BambuFTPClient._mode_cache.clear()
    BambuFTPClient._last_connect_failure.clear()


# -- the client knows what went wrong ------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), "tls"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionRefusedError("refused"), "network"),
    ],
)
def test_a_failed_connect_leaves_its_kind_on_the_client(error, kind):
    ftp = MagicMock()
    ftp.connect.side_effect = error
    client = BambuFTPClient("10.0.0.9", "12345678")
    with (
        patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=ftp),
        patch.object(bambu_ftp, "_read_cleartext_reply", return_value=None),
    ):
        assert client.connect() is False
    assert client.failure.kind == kind


def test_a_refused_login_is_auth():
    ftp = MagicMock()
    ftp.login.side_effect = ftplib.error_perm("530 Login incorrect.")
    client = BambuFTPClient("10.0.0.9", "12345678")
    with patch.object(bambu_ftp, "ImplicitFTP_TLS", return_value=ftp):
        client.connect()
    assert client.failure.kind == "auth"


def _connected_client(tmp_path: Path, stor_error: Exception) -> tuple[BambuFTPClient, Path]:
    local = tmp_path / "plate.gcode.3mf"
    local.write_bytes(b"PK" + b"0" * 64)
    client = BambuFTPClient("10.0.0.9", "12345678")
    client._ftp = MagicMock()
    client._ftp.transfercmd.side_effect = stor_error
    return client, local


@pytest.mark.parametrize(
    ("reply", "kind", "code"),
    [
        ("553 Could not create file.", "storage", "553"),
        ("552 Requested file action aborted. Exceeded storage allocation.", "storage", "552"),
        ("550 No such file or directory.", "not_found", "550"),
        ("501 Syntax error in parameters.", "rejected", "501"),
    ],
)
def test_the_printers_reply_is_kept(tmp_path, reply, kind, code):
    client, local = _connected_client(tmp_path, ftplib.error_perm(reply))
    assert client.upload_file(local, "/plate.gcode.3mf") is False
    assert (client.failure.kind, client.failure.code) == (kind, code)


def test_a_dropped_connection_is_a_transfer_failure(tmp_path):
    client, local = _connected_client(tmp_path, OSError("Connection reset by peer"))
    assert client.upload_file(local, "/plate.gcode.3mf") is False
    assert client.failure.kind == "transfer"


def test_a_short_copy_on_the_printer_is_truncated(tmp_path):
    local = tmp_path / "plate.gcode.3mf"
    local.write_bytes(b"PK" + b"0" * 64)
    client = BambuFTPClient("10.0.0.9", "12345678")
    client._ftp = MagicMock()
    client._ftp.transfercmd.return_value = MagicMock()
    with patch.object(client, "_uploaded_size_verdict", return_value=("truncated", 10)):
        assert client.upload_file(local, "/plate.gcode.3mf") is False
    assert client.failure.kind == "truncated"


# -- the caller gets it --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_report_carries_the_reason_to_the_caller(tmp_path):
    local = tmp_path / "plate.gcode.3mf"
    local.write_bytes(b"PK")

    def refused(self):
        self.failure = FtpFailure("tls", "WRONG_VERSION_NUMBER")
        return False

    report = UploadReport()
    with patch.object(BambuFTPClient, "connect", refused):
        assert await upload_file_async("10.0.0.9", "12345678", local, "/plate.gcode.3mf", report=report) is False
    assert report.failure.kind == "tls"


@pytest.mark.asyncio
async def test_two_callers_do_not_cross(tmp_path):
    """Why the report is the caller's and not the printer's."""
    local = tmp_path / "plate.gcode.3mf"
    local.write_bytes(b"PK")
    kinds = iter(["tls", "auth"])

    def refused(self):
        self.failure = FtpFailure(next(kinds))
        return False

    first, second = UploadReport(), UploadReport()
    with patch.object(BambuFTPClient, "connect", refused):
        await upload_file_async("10.0.0.9", "1", local, "/a.3mf", report=first)
        await upload_file_async("10.0.0.9", "1", local, "/b.3mf", report=second)
    assert (first.failure.kind, second.failure.kind) == ("tls", "auth")


@pytest.mark.asyncio
async def test_the_report_survives_the_retry_loop(tmp_path):
    local = tmp_path / "plate.gcode.3mf"
    local.write_bytes(b"PK")

    def refused(self):
        self.failure = FtpFailure("timeout")
        return False

    report = UploadReport()
    with patch.object(BambuFTPClient, "connect", refused):
        result = await bambu_ftp.with_ftp_retry(
            upload_file_async,
            "10.0.0.9",
            "12345678",
            local,
            "/plate.gcode.3mf",
            max_retries=1,
            retry_delay=0,
            report=report,
        )
    assert not result
    assert report.failure.kind == "timeout"


# -- one sentence per cause ----------------------------------------------------

_CARD_ADVICE = "formatted FAT32 or exFAT"


def test_storage_keeps_the_card_advice_and_the_code():
    text = describe_upload_failure(FtpFailure("storage", code="553"))
    assert _CARD_ADVICE in text and "553" in text


@pytest.mark.parametrize(
    ("failure", "says"),
    [
        (FtpFailure("tls"), "without TLS"),
        (FtpFailure("auth"), "access code"),
        (FtpFailure("timeout"), "did not answer in time"),
        (FtpFailure("network"), "could not be reached"),
        (FtpFailure("truncated"), "wrong size"),
        (FtpFailure("transfer"), "connection dropped"),
        (FtpFailure("not_found", code="550"), "550"),
        (FtpFailure("rejected", code="501"), "501"),
        (None, "server log"),
    ],
)
def test_every_other_cause_leaves_the_card_out(failure, says):
    text = describe_upload_failure(failure)
    assert says in text
    assert _CARD_ADVICE not in text


def test_the_dispatch_uses_the_reason_for_a_card_upload():
    from backend.app.services.background_dispatch import _upload_failure_message

    report = UploadReport(FtpFailure("tls"))
    assert _upload_failure_message("external", report) == describe_upload_failure(report.failure)
    assert "internal storage" in _upload_failure_message("internal", report)
