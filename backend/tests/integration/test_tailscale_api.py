"""Integration tests for GET /api/v1/virtual-printers/tailscale-status.

The endpoint is surface-only: it reports the host's Tailscale identity so the VP
card can show which IP / MagicDNS name to paste into the slicer. It must never
fail the request when Tailscale is absent — a missing binary or a wedged daemon
has to come back as a clean ``available: false`` payload, not a 500, because the
card renders it inline (#1070 post-rip-out).

``virtual_printers.get_tailscale_status`` imports ``tailscale_service`` inside
the function body, so these patch the singleton at its definition site rather
than an attribute on the route module.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

_SERVICE = "backend.app.services.virtual_printer.tailscale.tailscale_service"


class TestTailscaleStatusAPI:
    """Tests for the tailscale-status endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_tailscale_status_available(self, async_client: AsyncClient):
        """Returns 200 with the full identity when Tailscale is connected."""
        from backend.app.services.virtual_printer.tailscale import TailscaleStatus

        mock_status = TailscaleStatus(
            available=True,
            hostname="myhost",
            tailnet_name="example.ts.net",
            fqdn="myhost.example.ts.net",
            tailscale_ips=["100.1.2.3"],
        )

        with patch(f"{_SERVICE}.get_status", new=AsyncMock(return_value=mock_status)):
            response = await async_client.get("/api/v1/virtual-printers/tailscale-status")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] is True
        assert data["fqdn"] == "myhost.example.ts.net"
        assert data["hostname"] == "myhost"
        assert data["tailnet_name"] == "example.ts.net"
        assert data["tailscale_ips"] == ["100.1.2.3"]
        assert data["error"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_tailscale_status_unavailable(self, async_client: AsyncClient):
        """A missing binary is a 200 with available=false, never an error status.

        This is the default state for every install that doesn't run Tailscale —
        including, until the CLI was added to the image, every Docker install.
        """
        from backend.app.services.virtual_printer.tailscale import TailscaleStatus

        mock_status = TailscaleStatus(
            available=False,
            hostname="",
            tailnet_name="",
            fqdn="",
            error="tailscale binary not found",
        )

        with patch(f"{_SERVICE}.get_status", new=AsyncMock(return_value=mock_status)):
            response = await async_client.get("/api/v1/virtual-printers/tailscale-status")

        assert response.status_code == 200
        data = response.json()
        assert data["available"] is False
        assert data["fqdn"] == ""
        assert data["tailscale_ips"] == []
        assert data["error"] == "tailscale binary not found"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_wedged_daemon_still_returns_200(self, async_client: AsyncClient):
        """A daemon that times out must degrade to "unavailable", not a 500.

        ``get_status`` catches ``asyncio.TimeoutError`` from its 5 s bound; this
        guards the route contract that depends on it (upstream v0.2.4.5).
        """
        from backend.app.services.virtual_printer.tailscale import TailscaleStatus

        mock_status = TailscaleStatus(
            available=False,
            hostname="",
            tailnet_name="",
            fqdn="",
            error="Tailscale status timed out",
        )

        with patch(f"{_SERVICE}.get_status", new=AsyncMock(return_value=mock_status)):
            response = await async_client.get("/api/v1/virtual-printers/tailscale-status")

        assert response.status_code == 200
        assert response.json()["available"] is False
