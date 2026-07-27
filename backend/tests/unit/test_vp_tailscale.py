"""Tests for the VP Tailscale status wrapper."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.virtual_printer.tailscale import TailscaleService


class TestGetStatusErrorHandling:
    """get_status must degrade to an 'unavailable' card, never propagate."""

    @pytest.mark.asyncio
    async def test_timeout_returns_unavailable_not_raises(self):
        """A wedged ``tailscale status`` (TimeoutError re-raised by
        _run_tailscale) must be swallowed into an unavailable status instead
        of escaping to the /tailscale-status route as a 500 (upstream v0.2.4.5)."""
        svc = TailscaleService()
        with (
            patch("shutil.which", return_value="/usr/bin/tailscale"),
            patch.object(svc, "_run_tailscale", AsyncMock(side_effect=TimeoutError())),
        ):
            status = await svc.get_status()
        assert status.available is False
        assert status.error  # non-empty diagnostic

    @pytest.mark.asyncio
    async def test_oserror_returns_unavailable(self):
        svc = TailscaleService()
        with (
            patch("shutil.which", return_value="/usr/bin/tailscale"),
            patch.object(svc, "_run_tailscale", AsyncMock(side_effect=OSError("boom"))),
        ):
            status = await svc.get_status()
        assert status.available is False
        assert "boom" in status.error
