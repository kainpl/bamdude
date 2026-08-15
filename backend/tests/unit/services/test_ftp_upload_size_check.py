"""What we check about a file after handing it to the printer.

Until now: nothing. The two paths where a 3MF *arrives* both gained a ZIP check
after uvloop silently truncated one (``inv-vp-ftp-asyncio-loop``); the path it
*leaves* by had no check at all. A 226 says the printer finished reading the
stream, not that what it stored is what we sent.

⚠️ Size, not a hash. The printer's FTP exposes no digest of what it stored, so
bit-rot stays out of reach. A transfer that stopped early does not — and that is
the failure this codebase has actually had.

⚠️ Three verdicts, not two. A printer whose FTP has no ``SIZE`` must not read as
a failed upload; ``unknown`` leaves the caller where it was before the probe
existed. Turning it into a failure would break uploads that work today, to
protect against a truncation nobody has evidence of on that model.
"""

from __future__ import annotations

import ftplib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_ftp import BambuFTPClient

PAYLOAD = b"x" * 5000


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    p = tmp_path / "plate.3mf"
    p.write_bytes(PAYLOAD)
    return p


def _client(*, server_size: int | None, voidresp: Exception | None = None) -> BambuFTPClient:
    """A client whose transfer always succeeds, so only the checks after it vary."""
    c = BambuFTPClient("192.168.0.9", "00000000")
    ftp = MagicMock()
    conn = MagicMock()
    ftp.transfercmd.return_value = conn
    ftp.sock.gettimeout.return_value = 30
    if voidresp is not None:
        ftp.voidresp.side_effect = voidresp
    else:
        ftp.voidresp.return_value = "226 Transfer complete"
    if server_size is None:
        ftp.size.side_effect = ftplib.error_perm("500 Unknown command")
    else:
        ftp.size.return_value = server_size
    c._ftp = ftp
    return c


class TestTheAcknowledgedUpload:
    def test_a_matching_size_is_accepted(self, local_file: Path) -> None:
        assert _client(server_size=len(PAYLOAD)).upload_file(local_file, "/plate.3mf") is True

    def test_a_short_file_is_refused_even_though_the_printer_said_226(self, local_file: Path) -> None:
        """⚠️ The case that had no check at all. Returning True here is what puts
        a truncated 3MF in front of start_print, which fails a plate in rather
        than at the door."""
        assert _client(server_size=len(PAYLOAD) - 100).upload_file(local_file, "/plate.3mf") is False

    def test_a_printer_that_cannot_answer_SIZE_still_uploads(self, local_file: Path) -> None:
        """No probe, no verdict, no regression — this is how every model that
        lacks SIZE keeps working."""
        assert _client(server_size=None).upload_file(local_file, "/plate.3mf") is True


class TestWhenTheConfirmationNeverArrives:
    """No 226 read. The old code proceeded on the reasoning that "the data was
    sent on our side and the printer may still have written the file" — which is
    a guess the probe can settle."""

    def test_it_proceeds_when_the_size_is_right(self, local_file: Path) -> None:
        """H2D can take 30+ s to send 226; a timeout here is routine and the
        file is usually fine. Now that is checked rather than assumed."""
        client = _client(server_size=len(PAYLOAD), voidresp=TimeoutError("no 226"))

        assert client.upload_file(local_file, "/plate.3mf") is True

    def test_it_refuses_when_the_size_is_wrong(self, local_file: Path) -> None:
        """⚠️ The worst path to be wrong on: nothing confirmed the write AND the
        copy is short."""
        client = _client(server_size=1, voidresp=TimeoutError("no 226"))

        assert client.upload_file(local_file, "/plate.3mf") is False

    def test_it_still_proceeds_when_the_size_is_unknowable(self, local_file: Path) -> None:
        client = _client(server_size=None, voidresp=TimeoutError("no 226"))

        assert client.upload_file(local_file, "/plate.3mf") is True


class TestWhenThePrinterRejectsTheTransfer:
    """The branch that already probed — 426 on some P2S firmware, which is
    either a truncated file or noise from the TLS close racing the 226."""

    def test_a_matching_size_makes_it_noise(self, local_file: Path) -> None:
        client = _client(server_size=len(PAYLOAD), voidresp=ftplib.error_temp("426 Failure reading network stream"))

        assert client.upload_file(local_file, "/plate.3mf") is True

    def test_a_mismatch_makes_it_real(self, local_file: Path) -> None:
        client = _client(server_size=17, voidresp=ftplib.error_temp("426 Failure reading network stream"))

        assert client.upload_file(local_file, "/plate.3mf") is False


class TestTheProbeItself:
    def test_it_asks_the_printer_exactly_once(self, local_file: Path) -> None:
        """⚠️ Budget. This runs on the session that uploaded, and a second
        round-trip to this daemon is not free — a separate connection for the
        check is how the printer's FTP was wedged during the tunnel work. The
        verdict carries the size so no caller asks twice."""
        client = _client(server_size=len(PAYLOAD))

        client.upload_file(local_file, "/plate.3mf")

        assert client._ftp.size.call_count == 1

    def test_the_verdict_reports_what_it_saw(self) -> None:
        client = _client(server_size=4096)

        assert client._uploaded_size_verdict("/f.3mf", 4096) == ("ok", 4096)
        assert client._uploaded_size_verdict("/f.3mf", 9000) == ("truncated", 4096)

    def test_an_unanswerable_probe_carries_no_size(self) -> None:
        assert _client(server_size=None)._uploaded_size_verdict("/f.3mf", 10) == ("unknown", None)
