"""Tests for the Tailscale subprocess wrapper used by virtual printers (#1070)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.app.services.virtual_printer.tailscale import (
    TS_CERT_EXPIRY_THRESHOLD_DAYS,
    TailscaleService,
)


def _write_cert(path: Path, fqdn: str, days_remaining: int) -> None:
    """Helper: write a self-signed cert at ``path`` with the given SAN + lifetime."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, fqdn)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_remaining))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(fqdn)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_binary_missing_returns_unavailable(self):
        svc = TailscaleService()
        with patch("backend.app.services.virtual_printer.tailscale.shutil.which", return_value=None):
            status = await svc.get_status()
        assert status.available is False
        assert "tailscale binary not found" in (status.error or "")

    @pytest.mark.asyncio
    async def test_returns_fqdn_from_status_json(self):
        svc = TailscaleService()
        payload = json.dumps({"Self": {"DNSName": "myhost.tailnet.ts.net.", "TailscaleIPs": ["100.64.0.1"]}}).encode()
        with (
            patch("backend.app.services.virtual_printer.tailscale.shutil.which", return_value="/usr/bin/tailscale"),
            patch.object(svc, "_run_tailscale", AsyncMock(return_value=(0, payload, b""))),
        ):
            status = await svc.get_status()
        assert status.available is True
        assert status.hostname == "myhost"
        assert status.tailnet_name == "tailnet.ts.net"
        assert status.fqdn == "myhost.tailnet.ts.net"
        assert status.tailscale_ips == ["100.64.0.1"]

    @pytest.mark.asyncio
    async def test_daemon_returns_nonzero_marks_unavailable(self):
        svc = TailscaleService()
        with (
            patch("backend.app.services.virtual_printer.tailscale.shutil.which", return_value="/usr/bin/tailscale"),
            patch.object(svc, "_run_tailscale", AsyncMock(return_value=(1, b"", b"daemon down"))),
        ):
            status = await svc.get_status()
        assert status.available is False
        assert "daemon down" in (status.error or "")


class TestProvisionCert:
    @pytest.mark.asyncio
    async def test_rejects_invalid_fqdn(self, tmp_path: Path):
        svc = TailscaleService()
        ok = await svc.provision_cert("not a fqdn", tmp_path / "cert.pem", tmp_path / "key.pem")
        assert ok is False

    @pytest.mark.asyncio
    async def test_failure_returns_false(self, tmp_path: Path):
        svc = TailscaleService()
        with patch.object(svc, "_run_tailscale", AsyncMock(return_value=(1, b"", b"some error"))):
            ok = await svc.provision_cert("ok.tailnet.ts.net", tmp_path / "cert.pem", tmp_path / "key.pem")
        assert ok is False

    @pytest.mark.asyncio
    async def test_https_disabled_logs_friendly_warning(self, tmp_path: Path, caplog):
        svc = TailscaleService()
        with patch.object(
            svc,
            "_run_tailscale",
            AsyncMock(return_value=(1, b"", b"HTTPS cert is not enabled for this tailnet")),
        ):
            ok = await svc.provision_cert("ok.tailnet.ts.net", tmp_path / "cert.pem", tmp_path / "key.pem")
        assert ok is False
        assert any("HTTPS certs are not enabled" in r.getMessage() for r in caplog.records)


class TestCertNeedsRenewal:
    def test_missing_cert_needs_renewal(self, tmp_path: Path):
        svc = TailscaleService()
        assert svc.cert_needs_renewal(tmp_path / "missing.pem", fqdn="x.ts.net") is True

    def test_fresh_cert_does_not_need_renewal(self, tmp_path: Path):
        svc = TailscaleService()
        cert_path = tmp_path / "cert.pem"
        _write_cert(cert_path, fqdn="ok.ts.net", days_remaining=TS_CERT_EXPIRY_THRESHOLD_DAYS + 30)
        assert svc.cert_needs_renewal(cert_path, fqdn="ok.ts.net") is False

    def test_expiring_cert_needs_renewal(self, tmp_path: Path):
        svc = TailscaleService()
        cert_path = tmp_path / "cert.pem"
        _write_cert(cert_path, fqdn="ok.ts.net", days_remaining=TS_CERT_EXPIRY_THRESHOLD_DAYS - 5)
        assert svc.cert_needs_renewal(cert_path, fqdn="ok.ts.net") is True

    def test_san_mismatch_needs_renewal(self, tmp_path: Path):
        svc = TailscaleService()
        cert_path = tmp_path / "cert.pem"
        _write_cert(cert_path, fqdn="old.ts.net", days_remaining=TS_CERT_EXPIRY_THRESHOLD_DAYS + 30)
        assert svc.cert_needs_renewal(cert_path, fqdn="new.ts.net") is True


class TestEnsureCert:
    @pytest.mark.asyncio
    async def test_skips_provisioning_when_fresh(self, tmp_path: Path):
        svc = TailscaleService()
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        _write_cert(cert_path, fqdn="ok.ts.net", days_remaining=TS_CERT_EXPIRY_THRESHOLD_DAYS + 30)
        with patch.object(svc, "provision_cert", AsyncMock(return_value=True)) as mock_prov:
            ok = await svc.ensure_cert("ok.ts.net", cert_path, key_path)
        assert ok is True
        mock_prov.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provisions_when_renewal_needed(self, tmp_path: Path):
        svc = TailscaleService()
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        with patch.object(svc, "provision_cert", AsyncMock(return_value=True)) as mock_prov:
            ok = await svc.ensure_cert("ok.ts.net", cert_path, key_path)
        assert ok is True
        mock_prov.assert_awaited_once_with("ok.ts.net", cert_path, key_path)
