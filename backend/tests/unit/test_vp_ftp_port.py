"""Tests for Virtual Printer FTP server port configuration."""

from backend.app.services.virtual_printer.ftp_server import FTP_PORT


class TestFTPPort:
    """Verify FTP server uses the standard FTPS port."""

    def test_ftp_port_is_990(self):
        """FTP must bind to port 990 (standard implicit FTPS).

        Port 9990 required an iptables REDIRECT rule which rewrites
        the destination IP to the interface's primary address, breaking
        multi-VP setups with different bind IPs and access codes.
        """
        assert FTP_PORT == 990, (
            f"FTP_PORT must be 990 (standard FTPS), not {FTP_PORT}. "
            "Using a non-standard port requires iptables REDIRECT which "
            "breaks multi-VP setups."
        )
