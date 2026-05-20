"""Unit tests for the bed-jog and home-axes endpoints (#791, §17).

Tests:
  POST /api/v1/printers/{printer_id}/bed-jog?distance=<mm>&force=<bool>
  POST /api/v1/printers/{printer_id}/home-axes?axes=<z|xy|all>
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient


class TestBedJogAPI:
    @pytest.mark.asyncio
    async def test_bed_jog_not_found(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/printers/99999/bed-jog?distance=10")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_bed_jog_zero_distance_rejected(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="P1")
        response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=0")
        assert response.status_code == 400
        assert "distance" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_bed_jog_too_large_rejected(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="P1")
        response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=500")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_bed_jog_not_connected(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="Disconnected")
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=10")
            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_bed_jog_send_failure(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="P1")
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = False
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=10")
            assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_bed_jog_success_without_force(self, async_client: AsyncClient, printer_factory):
        """When force=false the M211 guard lines must not be emitted."""
        printer = await printer_factory(name="P1")
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = True
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=10&force=false")
            assert response.status_code == 200
            sent_gcode = mock_client.send_gcode.call_args[0][0]
            assert "G91" in sent_gcode
            assert "G1 Z10.00" in sent_gcode
            assert "G90" in sent_gcode
            assert "M211" not in sent_gcode

    @pytest.mark.asyncio
    async def test_bed_jog_success_with_force(self, async_client: AsyncClient, printer_factory):
        """force=true must wrap the move in M211 S0 / M211 S1."""
        printer = await printer_factory(name="P1")
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = True
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=-5&force=true")
            assert response.status_code == 200
            sent_gcode = mock_client.send_gcode.call_args[0][0]
            lines = sent_gcode.splitlines()
            assert lines[0] == "M211 S0"
            assert lines[-1] == "M211 S1"
            assert "G1 Z-5.00" in sent_gcode

    # --- Direction-flip regression on bed-slingers (upstream #1334) ---
    #
    # The UI maps "Up arrow = decrease nozzle-bed gap" to a negative distance
    # (the X1/P1/H2 bed-on-Z convention, where Z=0 is at the top and Z+ moves
    # the bed down). On A1 / A1 Mini bed-slingers the Z-axis controls the
    # *toolhead* — same `G1 Z-` literal would drive the nozzle straight into
    # the bed. The backend therefore inverts the signed distance on A1 family
    # printers so the UI contract stays consistent.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        ["X1C", "X1E", "P1S", "P1P", "H2D", "H2S", "H2C", "P2S"],
    )
    async def test_bed_on_z_models_pass_through(self, async_client: AsyncClient, printer_factory, model):
        """Every bed-on-Z model still emits the literal sign the UI sent."""
        printer = await printer_factory(name=f"BedOnZ-{model}", model=model)
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = True
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            # UI "Up" = negative distance = decrease nozzle-bed gap
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=-10")
            assert response.status_code == 200
            sent_gcode = mock_client.send_gcode.call_args[0][0]
            assert "G1 Z-10.00" in sent_gcode, f"{model} should pass through the negative sign"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        ["A1", "A1 Mini", "A1MINI", "A1-MINI", "a1", "N1", "N2S"],
    )
    async def test_bed_slinger_models_invert_sign(self, async_client: AsyncClient, printer_factory, model):
        """A1 family inverts the sign so UI "Up" drives the toolhead UP, not into the bed."""
        printer = await printer_factory(name=f"Slinger-{model}", model=model)
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = True
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            # UI "Up" = negative distance, but on bed-slinger we want toolhead UP
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=-10")
            assert response.status_code == 200
            sent_gcode = mock_client.send_gcode.call_args[0][0]
            assert "G1 Z10.00" in sent_gcode, f"{model} should invert the sign (toolhead goes up)"

    @pytest.mark.asyncio
    async def test_bed_slinger_down_arrow_drops_toolhead(self, async_client: AsyncClient, printer_factory):
        """Symmetric: UI "Down arrow" (positive distance) on A1 produces G1 Z-, dropping the toolhead toward the bed."""
        printer = await printer_factory(name="A1-down", model="A1")
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = True
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            response = await async_client.post(f"/api/v1/printers/{printer.id}/bed-jog?distance=5")
            assert response.status_code == 200
            sent_gcode = mock_client.send_gcode.call_args[0][0]
            assert "G1 Z-5.00" in sent_gcode


class TestHomeAxesAPI:
    @pytest.mark.asyncio
    async def test_home_axes_not_found(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/printers/99999/home-axes?axes=z")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_home_axes_invalid(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="P1")
        response = await async_client.post(f"/api/v1/printers/{printer.id}/home-axes?axes=bogus")
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("axes", ["z", "xy", "all"])
    async def test_home_axes_always_runs_full_home(self, async_client: AsyncClient, printer_factory, axes):
        # Regression for upstream #1052: regardless of the axes argument, the endpoint must send
        # a bare `G28` so the printer's safe auto-home sequence (park toolhead → home XY → home Z)
        # runs. Sending `G28 Z` alone on H2C/H2D/H2S/X1 can crash the bed into the toolhead.
        printer = await printer_factory(name="P1")
        mock_client = MagicMock()
        mock_client.send_gcode.return_value = True
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            response = await async_client.post(f"/api/v1/printers/{printer.id}/home-axes?axes={axes}")
            assert response.status_code == 200
            mock_client.send_gcode.assert_called_once_with("G28")

    @pytest.mark.asyncio
    async def test_home_axes_not_connected(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="D")
        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None
            response = await async_client.post(f"/api/v1/printers/{printer.id}/home-axes?axes=z")
            assert response.status_code == 400
