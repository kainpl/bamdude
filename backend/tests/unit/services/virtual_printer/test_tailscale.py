"""Tests for the Tailscale presence-detection wrapper used by virtual printers
(#1070, post-rip-out).

The original module also provisioned Let's Encrypt certs via ``tailscale cert``;
those paths were deleted because BambuStudio / OrcaSlicer's printer-MQTT trust
path validates only against its bundled BBL CA, so an LE-signed cert is
rejected regardless of hostname. Only ``get_status`` survives — its sole job
is to surface the host's Tailscale IP / FQDN so the VP card can show users
what to paste into the slicer's Add Printer dialog.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.virtual_printer.tailscale import TailscaleService


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

    @pytest.mark.asyncio
    async def test_empty_dnsname_marks_unavailable(self):
        """Tailscale daemon up but machine not yet on the tailnet → no DNSName."""
        svc = TailscaleService()
        payload = json.dumps({"Self": {"DNSName": "", "TailscaleIPs": []}}).encode()
        with (
            patch("backend.app.services.virtual_printer.tailscale.shutil.which", return_value="/usr/bin/tailscale"),
            patch.object(svc, "_run_tailscale", AsyncMock(return_value=(0, payload, b""))),
        ):
            status = await svc.get_status()
        assert status.available is False
        assert "no DNSName" in (status.error or "")

    @pytest.mark.asyncio
    async def test_malformed_json_marks_unavailable(self):
        svc = TailscaleService()
        with (
            patch("backend.app.services.virtual_printer.tailscale.shutil.which", return_value="/usr/bin/tailscale"),
            patch.object(svc, "_run_tailscale", AsyncMock(return_value=(0, b"not json", b""))),
        ):
            status = await svc.get_status()
        assert status.available is False
        assert "JSON parse error" in (status.error or "")
