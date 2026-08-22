"""Integration tests for Printers API endpoints.

Tests the full request/response cycle for /api/v1/printers/ endpoints.
"""

from unittest.mock import AsyncMock, MagicMock, patch as _orig_patch

import pytest
from httpx import AsyncClient


class _PrinterManagerPatch:
    """Context manager wrapping patch that auto-adds AsyncMock for ensure_fresh methods."""

    def __init__(self, target, **kwargs):
        self._patcher = _orig_patch(target, **kwargs)
        self._is_pm = "printer_manager" in target

    def __enter__(self):
        mock = self._patcher.__enter__()
        if self._is_pm:
            mock.ensure_fresh_connection = AsyncMock(return_value=True)
            mock.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
        return mock

    def __exit__(self, *args):
        return self._patcher.__exit__(*args)


patch = _PrinterManagerPatch  # noqa: E811


class TestPrintersAPI:
    """Integration tests for /api/v1/printers/ endpoints."""

    @pytest.fixture(autouse=True)
    def _mock_connection_probe(self):
        """Add-Printer now probes MQTT connectivity before persisting (the
        empty-card fix). Default the probe to success so the existing create
        tests exercise the create flow; the probe-failure path is tested
        explicitly below."""
        with _orig_patch(
            "backend.app.api.routes.printers.printer_manager.test_connection",
            new=AsyncMock(return_value={"success": True, "state": "IDLE", "model": "X1C"}),
        ):
            yield

    # ========================================================================
    # List endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_printers_empty(self, async_client: AsyncClient):
        """Verify empty list is returned when no printers exist."""
        response = await async_client.get("/api/v1/printers/")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_printers_with_data(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify list returns existing printers."""
        await printer_factory(name="Test Printer")

        response = await async_client.get("/api/v1/printers/")

        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(p["name"] == "Test Printer" for p in data)

    # ========================================================================
    # Create endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_printer(self, async_client: AsyncClient):
        """Verify printer can be created."""
        data = {
            "name": "New Printer",
            "serial_number": "00M09A111111111",
            "ip_address": "192.168.1.100",
            "access_code": "12345678",
            "is_active": True,
            "model": "X1C",
        }

        response = await async_client.post("/api/v1/printers/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "New Printer"
        assert result["serial_number"] == "00M09A111111111"
        assert result["model"] == "X1C"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_printer_with_hostname(self, async_client: AsyncClient):
        """Verify printer can be created with a hostname instead of IP address."""
        data = {
            "name": "DNS Printer",
            "serial_number": "00M09A555555555",
            "ip_address": "printer.local",
            "access_code": "12345678",
            "model": "P1S",
        }

        response = await async_client.post("/api/v1/printers/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "DNS Printer"
        assert result["ip_address"] == "printer.local"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_printer_with_fqdn(self, async_client: AsyncClient):
        """Verify printer can be created with a fully qualified domain name."""
        data = {
            "name": "FQDN Printer",
            "serial_number": "00M09A666666666",
            "ip_address": "my-printer.home.lan",
            "access_code": "12345678",
            "model": "X1C",
        }

        response = await async_client.post("/api/v1/printers/", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["ip_address"] == "my-printer.home.lan"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_printer_invalid_hostname(self, async_client: AsyncClient):
        """Verify invalid hostnames are rejected."""
        data = {
            "name": "Bad Printer",
            "serial_number": "00M09A777777777",
            "ip_address": "-invalid",
            "access_code": "12345678",
        }

        response = await async_client.post("/api/v1/printers/", json=data)

        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_printer_duplicate_serial(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify duplicate serial number is rejected."""
        await printer_factory(serial_number="00M09A222222222")

        data = {
            "name": "Duplicate Printer",
            "serial_number": "00M09A222222222",
            "ip_address": "192.168.1.101",
            "access_code": "12345678",
        }

        response = await async_client.post("/api/v1/printers/", json=data)

        # Should fail due to duplicate serial
        assert response.status_code in [400, 409, 422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_created_printer_defaults_not_archived(self, async_client: AsyncClient):
        """A freshly created printer serializes archived=False / archived_at=None."""
        data = {
            "name": "Arch A",
            "serial_number": "ARCH0001",
            "ip_address": "10.0.0.9",
            "access_code": "12345678",
            "model": "X1C",
        }
        resp = await async_client.post("/api/v1/printers/", json=data)
        assert resp.status_code in (200, 201)
        body = resp.json()
        assert body["archived"] is False
        assert body["archived_at"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_excludes_archived_by_default(self, async_client: AsyncClient, printer_factory, db_session):
        """GET /printers/ hides archived printers unless include_archived=true."""
        await printer_factory(name="Live", serial_number="LIVE01")
        p_arch = await printer_factory(name="Retired", serial_number="ARCH02")
        p_arch.archived = True
        await db_session.commit()

        default = await async_client.get("/api/v1/printers/")
        names = {p["name"] for p in default.json()}
        assert "Live" in names and "Retired" not in names

        incl = await async_client.get("/api/v1/printers/?include_archived=true")
        names_incl = {p["name"] for p in incl.json()}
        assert "Live" in names_incl and "Retired" in names_incl

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_sets_flag_and_cancels_pending(self, async_client: AsyncClient, printer_factory, db_session):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_queue import PrinterQueue

        p = await printer_factory(name="ToArchive", serial_number="ARCH03")
        queue = PrinterQueue(printer_id=p.id)
        db_session.add(queue)
        await db_session.commit()
        await db_session.refresh(queue)
        db_session.add(PrintQueueItem(queue_id=queue.id, status="pending", position=1))
        db_session.add(PrintQueueItem(queue_id=queue.id, status="pending", position=2))
        await db_session.commit()

        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.is_print_active.return_value = False
            resp = await async_client.post(f"/api/v1/printers/{p.id}/archive")

        assert resp.status_code == 200
        body = resp.json()
        assert body["archived"] is True
        assert body["cancelled_items"] == 2
        pm.disconnect_printer.assert_called_once_with(p.id)
        await db_session.refresh(p)
        assert p.archived is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_blocked_while_printing(self, async_client: AsyncClient, printer_factory, db_session):
        p = await printer_factory(name="Busy", serial_number="ARCH04")
        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.is_print_active.return_value = True
            resp = await async_client.post(f"/api/v1/printers/{p.id}/archive")
        assert resp.status_code == 409
        await db_session.refresh(p)
        assert p.archived is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unarchive_clears_flag_and_reconnects_if_active(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        p = await printer_factory(name="Back", serial_number="ARCH05")
        p.archived = True
        p.is_active = True
        await db_session.commit()
        with patch("backend.app.api.routes.printers.printer_manager") as pm:
            pm.connect_printer = AsyncMock(return_value=True)
            resp = await async_client.post(f"/api/v1/printers/{p.id}/unarchive")
        assert resp.status_code == 200
        assert resp.json()["archived"] is False
        pm.connect_printer.assert_awaited_once()
        await db_session.refresh(p)
        assert p.archived is False and p.archived_at is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_readd_archived_serial_hints_unarchive(self, async_client: AsyncClient, printer_factory, db_session):
        """Re-adding a printer whose serial matches an archived one returns a
        409 pointing at unarchive rather than a generic duplicate error."""
        p = await printer_factory(name="Old", serial_number="DUP999")
        p.archived = True
        await db_session.commit()
        data = {
            "name": "New",
            "serial_number": "DUP999",
            "ip_address": "10.0.0.5",
            "access_code": "12345678",
            "model": "X1C",
        }
        resp = await async_client.post("/api/v1/printers/", json=data)
        assert resp.status_code == 409
        assert "archiv" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_active_printer_rejected_when_probe_fails(self, async_client: AsyncClient):
        """A failed MQTT probe (mistyped access code / wrong IP) returns 400 and
        does NOT persist the printer — no empty card on the dashboard."""
        data = {
            "name": "Unreachable",
            "serial_number": "00M09A333333333",
            "ip_address": "192.168.1.199",
            "access_code": "00000000",
            "is_active": True,
            "model": "X1C",
        }
        with _orig_patch(
            "backend.app.api.routes.printers.printer_manager.test_connection",
            new=AsyncMock(return_value={"success": False}),
        ):
            response = await async_client.post("/api/v1/printers/", json=data)
        assert response.status_code == 400

        # The row must not have been written.
        listing = await async_client.get("/api/v1/printers/")
        serials = [p["serial_number"] for p in listing.json()]
        assert "00M09A333333333" not in serials

    # ========================================================================
    # Get single endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_printer(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify single printer can be retrieved."""
        printer = await printer_factory(name="Get Test Printer")

        response = await async_client.get(f"/api/v1/printers/{printer.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["id"] == printer.id
        assert result["name"] == "Get Test Printer"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_printer_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.get("/api/v1/printers/9999")

        assert response.status_code == 404

    # ========================================================================
    # Update endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_printer_name(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify printer name can be updated."""
        printer = await printer_factory(name="Original Name")

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"name": "Updated Name"})

        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_printer_active_status(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify printer active status can be updated."""
        printer = await printer_factory(is_active=True)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"is_active": False})

        assert response.status_code == 200
        assert response.json()["is_active"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_printer_auto_archive(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify auto_archive setting can be updated."""
        printer = await printer_factory(auto_archive=True)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"auto_archive": False})

        assert response.status_code == 200
        assert response.json()["auto_archive"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_nonexistent_printer(self, async_client: AsyncClient):
        """Verify updating non-existent printer returns 404."""
        response = await async_client.patch("/api/v1/printers/9999", json={"name": "New Name"})

        assert response.status_code == 404

    # ========================================================================
    # Delete endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_printer(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify printer can be deleted."""
        printer = await printer_factory()
        printer_id = printer.id

        response = await async_client.delete(f"/api/v1/printers/{printer_id}")

        assert response.status_code == 200

        # Verify deleted
        response = await async_client.get(f"/api/v1/printers/{printer_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_nonexistent_printer(self, async_client: AsyncClient):
        """Verify deleting non-existent printer returns 404."""
        response = await async_client.delete("/api/v1/printers/9999")

        assert response.status_code == 404

    # ========================================================================
    # Status endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_printer_status(
        self, async_client: AsyncClient, printer_factory, mock_printer_manager, db_session
    ):
        """Verify printer status can be retrieved."""
        printer = await printer_factory()

        response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert response.status_code == 200
        result = response.json()
        assert "connected" in result
        assert "state" in result

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_printer_status_not_found(self, async_client: AsyncClient):
        """Verify 404 for status of non-existent printer."""
        response = await async_client.get("/api/v1/printers/9999/status")

        assert response.status_code == 404

    # ========================================================================
    # Test connection endpoint
    # ========================================================================


class TestPrinterDataIntegrity:
    """Tests for printer data integrity."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_printer_stores_all_fields(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify printer stores all fields correctly."""
        printer = await printer_factory(
            name="Full Test Printer",
            serial_number="00M09A444444444",
            ip_address="192.168.1.150",
            model="P1S",
            is_active=True,
            auto_archive=False,
        )

        response = await async_client.get(f"/api/v1/printers/{printer.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "Full Test Printer"
        assert result["serial_number"] == "00M09A444444444"
        assert result["ip_address"] == "192.168.1.150"
        assert result["model"] == "P1S"
        assert result["is_active"] is True
        assert result["auto_archive"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_printer_update_persists(self, async_client: AsyncClient, printer_factory, db_session):
        """CRITICAL: Verify printer updates persist."""
        printer = await printer_factory(name="Original", is_active=True)

        # Update
        await async_client.patch(f"/api/v1/printers/{printer.id}", json={"name": "Updated", "is_active": False})

        # Verify persistence
        response = await async_client.get(f"/api/v1/printers/{printer.id}")
        result = response.json()
        assert result["name"] == "Updated"
        assert result["is_active"] is False

    # ========================================================================
    # Refresh status endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_refresh_status_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/refresh-status")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_refresh_status_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify 400 when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.request_status_update.return_value = False

            response = await async_client.post(f"/api/v1/printers/{printer.id}/refresh-status")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_refresh_status_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful refresh request."""
        printer = await printer_factory(name="Connected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.request_status_update.return_value = True

            response = await async_client.post(f"/api/v1/printers/{printer.id}/refresh-status")

            assert response.status_code == 200
            assert response.json()["status"] == "refresh_requested"
            mock_pm.request_status_update.assert_called_once_with(printer.id)

    # ========================================================================
    # Current print user endpoint (Issue #206)
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_current_print_user_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.get("/api/v1/printers/99999/current-print-user")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_current_print_user_returns_empty_when_no_user(self, async_client: AsyncClient, printer_factory):
        """Verify empty object returned when no user is tracked."""
        printer = await printer_factory(name="Test Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_current_print_user.return_value = None

            response = await async_client.get(f"/api/v1/printers/{printer.id}/current-print-user")

            assert response.status_code == 200
            assert response.json() == {}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_current_print_user_returns_user_info(self, async_client: AsyncClient, printer_factory):
        """Verify user info is returned when tracked."""
        printer = await printer_factory(name="Test Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_current_print_user.return_value = {"user_id": 42, "username": "testuser"}

            response = await async_client.get(f"/api/v1/printers/{printer.id}/current-print-user")

            assert response.status_code == 200
            result = response.json()
            assert result["user_id"] == 42
            assert result["username"] == "testuser"


class TestPrintControlAPI:
    """Integration tests for print control endpoints (stop, pause, resume)."""

    # ========================================================================
    # Stop print endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stop_print_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/print/stop")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stop_print_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/stop")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stop_print_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful stop print request."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.stop_print.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/stop")

            assert response.status_code == 200
            assert response.json()["success"] is True
            mock_client.stop_print.assert_called_once()

    # ========================================================================
    # Pause print endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pause_print_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/print/pause")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pause_print_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/pause")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pause_print_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful pause print request."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.pause_print.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/pause")

            assert response.status_code == 200
            assert response.json()["success"] is True
            mock_client.pause_print.assert_called_once()

    # ========================================================================
    # Resume print endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_resume_print_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/print/resume")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_resume_print_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/resume")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_resume_print_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful resume print request."""
        printer = await printer_factory(name="Paused Printer")

        mock_client = MagicMock()
        mock_client.resume_print.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/resume")

            assert response.status_code == 200
            assert response.json()["success"] is True
            mock_client.resume_print.assert_called_once()


class TestAMSRefreshAPI:
    """Integration tests for AMS slot refresh endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ams_refresh_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/ams/0/slot/0/refresh")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ams_refresh_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/ams/0/slot/0/refresh")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ams_refresh_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful AMS refresh request."""
        printer = await printer_factory(name="Printer with AMS")

        mock_client = MagicMock()
        mock_client.ams_refresh_tray.return_value = (True, "Refreshing AMS 0 tray 1")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/ams/0/slot/1/refresh")

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            mock_client.ams_refresh_tray.assert_called_once_with(0, 1)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ams_refresh_filament_loaded(self, async_client: AsyncClient, printer_factory):
        """Verify error when filament is loaded (can't refresh while loaded)."""
        printer = await printer_factory(name="Printer with AMS")

        mock_client = MagicMock()
        mock_client.ams_refresh_tray.return_value = (False, "Please unload filament first")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/ams/0/slot/0/refresh")

            assert response.status_code == 400
            assert "unload" in response.json()["detail"].lower()


class TestConfigureAMSSlotAPI:
    """Integration tests for AMS slot configure endpoint - tray_info_idx resolution."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_configure_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/slots/0/0/configure",
                params={
                    "tray_info_idx": "GFL99",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Basic",
                    "tray_color": "FF0000FF",
                    "nozzle_temp_min": 190,
                    "nozzle_temp_max": 230,
                },
            )

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_configure_with_gf_id_keeps_it(self, async_client: AsyncClient, printer_factory):
        """Standard Bambu GF* filament IDs are sent as-is."""
        printer = await printer_factory(name="H2D")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.request_status_update.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = None  # No existing state

            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/slots/2/3/configure",
                params={
                    "tray_info_idx": "GFL05",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Basic",
                    "tray_color": "FFFFFFFF",
                    "nozzle_temp_min": 190,
                    "nozzle_temp_max": 230,
                },
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL05"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_configure_pfus_sent_directly(self, async_client: AsyncClient, printer_factory):
        """A raw PFUS id degrades to the generic family (spec A §5.2)."""
        printer = await printer_factory(name="H2D")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.request_status_update.return_value = True

        mock_status = MagicMock()
        mock_status.raw_data = {"ams": {"ams": []}}  # No existing tray data

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = mock_status

            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/slots/2/3/configure",
                params={
                    "tray_info_idx": "PFUS9ac902733670a9",
                    "tray_type": "PLA",
                    "tray_sub_brands": "Devil Design PLA",
                    "tray_color": "FF0000FF",
                    "nozzle_temp_min": 190,
                    "nozzle_temp_max": 230,
                },
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # Family model (spec A §5.2): a raw PFUS setting-id is not a valid
            # tray id; unmirrored it degrades to the generic family of the
            # material rather than leaking to the printer.
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_configure_pfus_takes_priority_over_slot(self, async_client: AsyncClient, printer_factory):
        """Provided PFUS* preset takes priority over slot's existing preset."""
        printer = await printer_factory(name="H2D")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.request_status_update.return_value = True

        # Simulate slot already configured by slicer with cloud-synced preset
        mock_status = MagicMock()
        mock_status.raw_data = {
            "ams": {
                "ams": [
                    {
                        "id": 2,
                        "tray": [
                            {
                                "id": 3,
                                "tray_info_idx": "P4d64437",
                                "tray_type": "PLA",
                                "tray_color": "FF0000FF",
                            }
                        ],
                    }
                ]
            }
        }

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = mock_status

            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/slots/2/3/configure",
                params={
                    "tray_info_idx": "PFUS9ac902733670a9",
                    "tray_type": "PLA",
                    "tray_sub_brands": "Devil Design PLA",
                    "tray_color": "FF0000FF",
                    "nozzle_temp_min": 190,
                    "nozzle_temp_max": 230,
                },
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # Provided preset wins over slot's existing one
            # Family model (spec A §5.2): a raw PFUS setting-id is not a valid
            # tray id; unmirrored it degrades to the generic family of the
            # material rather than leaking to the printer.
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_configure_pfus_used_regardless_of_slot_material(self, async_client: AsyncClient, printer_factory):
        """Provided PFUS* preset is used even when slot has a different material."""
        printer = await printer_factory(name="H2D")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.request_status_update.return_value = True

        # Slot currently has PETG but user is configuring PLA
        mock_status = MagicMock()
        mock_status.raw_data = {
            "ams": {
                "ams": [
                    {
                        "id": 2,
                        "tray": [{"id": 3, "tray_info_idx": "GFG99", "tray_type": "PETG", "tray_color": "FFFFFFFF"}],
                    }
                ]
            }
        }

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = mock_status

            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/slots/2/3/configure",
                params={
                    "tray_info_idx": "PFUS9ac902733670a9",
                    "tray_type": "PLA",
                    "tray_sub_brands": "Devil Design PLA",
                    "tray_color": "FF0000FF",
                    "nozzle_temp_min": 190,
                    "nozzle_temp_max": 230,
                },
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            # Provided preset wins - slot's material is irrelevant
            # Family model (spec A §5.2): a raw PFUS setting-id is not a valid
            # tray id; unmirrored it degrades to the generic family of the
            # material rather than leaking to the printer.
            assert call_kwargs.kwargs["tray_info_idx"] == "GFL99"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_configure_empty_id_uses_generic(self, async_client: AsyncClient, printer_factory):
        """Empty tray_info_idx (local preset) is replaced with generic."""
        printer = await printer_factory(name="H2D")

        mock_client = MagicMock()
        mock_client.ams_set_filament_setting.return_value = True
        mock_client.extrusion_cali_sel.return_value = True
        mock_client.request_status_update.return_value = True

        mock_status = MagicMock()
        mock_status.raw_data = {"ams": {"ams": []}}

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client
            mock_pm.get_status.return_value = mock_status

            response = await async_client.post(
                f"/api/v1/printers/{printer.id}/slots/2/3/configure",
                params={
                    "tray_info_idx": "",
                    "tray_type": "PETG",
                    "tray_sub_brands": "PETG Basic",
                    "tray_color": "FFFFFFFF",
                    "nozzle_temp_min": 220,
                    "nozzle_temp_max": 260,
                },
            )

            assert response.status_code == 200
            call_kwargs = mock_client.ams_set_filament_setting.call_args
            assert call_kwargs.kwargs["tray_info_idx"] == "GFG99"


class TestSkipObjectsAPI:
    """Integration tests for skip objects endpoints."""

    # ========================================================================
    # Get printable objects endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_objects_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.get("/api/v1/printers/99999/print/objects")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_objects_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.get(f"/api/v1/printers/{printer.id}/print/objects")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_objects_empty(self, async_client: AsyncClient, printer_factory):
        """Verify empty objects list when no print is active."""
        printer = await printer_factory(name="Idle Printer")

        mock_client = MagicMock()
        mock_client.state.printable_objects = {}
        mock_client.state.skipped_objects = []
        mock_client.state.state = "IDLE"
        mock_client.state.subtask_name = None  # Prevent FTP download attempt

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.get(f"/api/v1/printers/{printer.id}/print/objects")

            assert response.status_code == 200
            result = response.json()
            assert result["objects"] == []
            assert result["total"] == 0
            assert result["skipped_count"] == 0
            assert result["is_printing"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_objects_with_data(self, async_client: AsyncClient, printer_factory):
        """Verify objects list when print is active."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.state.printable_objects = {100: "Part A", 200: "Part B", 300: "Part C"}
        mock_client.state.skipped_objects = [200]
        mock_client.state.state = "RUNNING"

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.get(f"/api/v1/printers/{printer.id}/print/objects")

            assert response.status_code == 200
            result = response.json()
            assert result["total"] == 3
            assert result["skipped_count"] == 1
            assert result["is_printing"] is True

            # Check objects have correct structure
            objects_by_id = {obj["id"]: obj for obj in result["objects"]}
            assert objects_by_id[100]["name"] == "Part A"
            assert objects_by_id[100]["skipped"] is False
            assert objects_by_id[200]["name"] == "Part B"
            assert objects_by_id[200]["skipped"] is True
            assert objects_by_id[300]["name"] == "Part C"
            assert objects_by_id[300]["skipped"] is False

    # ========================================================================
    # Skip objects endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_objects_with_positions(self, async_client: AsyncClient, printer_factory):
        """Verify objects list includes position data when available."""
        printer = await printer_factory(name="Printing Printer")

        # New format with position data
        mock_client = MagicMock()
        mock_client.state.printable_objects = {
            100: {"name": "Part A", "x": 50.0, "y": 100.0},
            200: {"name": "Part B", "x": 150.0, "y": 100.0},
        }
        mock_client.state.skipped_objects = []
        mock_client.state.state = "RUNNING"
        # Set explicitly: a MagicMock auto-attribute is truthy, so leaving these
        # off does not simulate "absent" — it hands the route a mock where a
        # bbox and a boolean belong.
        mock_client.state.printable_objects_bbox_all = [0.0, 0.0, 200.0, 200.0]
        mock_client.state.printable_objects_approximate = False

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.get(f"/api/v1/printers/{printer.id}/print/objects")

            assert response.status_code == 200
            result = response.json()
            assert result["total"] == 2

            # Check objects have position data
            objects_by_id = {obj["id"]: obj for obj in result["objects"]}
            assert objects_by_id[100]["name"] == "Part A"
            assert objects_by_id[100]["x"] == 50.0
            assert objects_by_id[100]["y"] == 100.0
            assert objects_by_id[200]["name"] == "Part B"
            assert objects_by_id[200]["x"] == 150.0
            assert objects_by_id[200]["y"] == 100.0

    # ========================================================================
    # Skip objects endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skip_objects_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/print/skip-objects", json=[100])
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skip_objects_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/skip-objects", json=[100])

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skip_objects_empty_list(self, async_client: AsyncClient, printer_factory):
        """Verify error when no object IDs provided."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.state.printable_objects = {100: "Part A"}
        mock_client.state.skipped_objects = []

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/skip-objects", json=[])

            assert response.status_code == 400
            assert "no object" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skip_objects_invalid_id(self, async_client: AsyncClient, printer_factory):
        """Verify error when object ID doesn't exist."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.state.printable_objects = {100: "Part A"}
        mock_client.state.skipped_objects = []

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/skip-objects", json=[999])

            assert response.status_code == 400
            assert "invalid" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skip_objects_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful skip objects request."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.state.printable_objects = {100: "Part A", 200: "Part B"}
        mock_client.state.skipped_objects = []
        mock_client.skip_objects.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/skip-objects", json=[100])

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert 100 in result["skipped_objects"]
            mock_client.skip_objects.assert_called_once_with([100])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skip_objects_multiple(self, async_client: AsyncClient, printer_factory):
        """Verify skipping multiple objects at once."""
        printer = await printer_factory(name="Printing Printer")

        mock_client = MagicMock()
        mock_client.state.printable_objects = {100: "Part A", 200: "Part B", 300: "Part C"}
        mock_client.state.skipped_objects = []
        mock_client.skip_objects.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/print/skip-objects", json=[100, 200])

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert 100 in result["skipped_objects"]
            assert 200 in result["skipped_objects"]
            mock_client.skip_objects.assert_called_once_with([100, 200])


class TestChamberLightAPI:
    """Integration tests for chamber light control endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chamber_light_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/chamber-light?on=true")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chamber_light_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/chamber-light?on=true")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chamber_light_on_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful chamber light on request."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.set_chamber_light.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/chamber-light?on=true")

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "on" in result["message"].lower()
            mock_client.set_chamber_light.assert_called_once_with(True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chamber_light_off_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful chamber light off request."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.set_chamber_light.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/chamber-light?on=false")

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "off" in result["message"].lower()
            mock_client.set_chamber_light.assert_called_once_with(False)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_chamber_light_failure(self, async_client: AsyncClient, printer_factory):
        """Verify error handling when chamber light control fails."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.set_chamber_light.return_value = False

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/chamber-light?on=true")

            assert response.status_code == 500
            assert "failed" in response.json()["detail"].lower()


class TestClearHMSErrorsAPI:
    """Integration tests for clear HMS errors endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clear_hms_errors_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent printer."""
        response = await async_client.post("/api/v1/printers/99999/hms/clear")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clear_hms_errors_not_connected(self, async_client: AsyncClient, printer_factory):
        """Verify error when printer is not connected."""
        printer = await printer_factory(name="Disconnected Printer")

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = None

            response = await async_client.post(f"/api/v1/printers/{printer.id}/hms/clear")

            assert response.status_code == 400
            assert "not connected" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clear_hms_errors_success(self, async_client: AsyncClient, printer_factory):
        """Verify successful clear HMS errors request."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.clear_hms_errors.return_value = True

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/hms/clear")

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert "cleared" in result["message"].lower()
            mock_client.clear_hms_errors.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clear_hms_errors_failure(self, async_client: AsyncClient, printer_factory):
        """Verify error handling when clear HMS errors fails."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.clear_hms_errors.return_value = False

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_client.return_value = mock_client

            response = await async_client.post(f"/api/v1/printers/{printer.id}/hms/clear")

            assert response.status_code == 500
            assert "failed" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_printer_status_includes_fila_switch_when_installed(
        self, async_client: AsyncClient, printer_factory
    ):
        """When the FTS accessory is installed, the status response must include
        the fila_switch object with the routing arrays. Upstream Bambuddy #1162.

        The accessory is detected from print.device.fila_switch in MQTT;
        we feed a PrinterState with FilaSwitchState(installed=True, ...) and
        confirm it survives the schema serialization round-trip.
        """
        from backend.app.services.bambu_mqtt import FilaSwitchState, PrinterState

        printer = await printer_factory()

        state = PrinterState()
        state.connected = True
        state.state = "IDLE"
        state.fila_switch = FilaSwitchState(
            installed=True,
            in_slots=[-1, 2],
            out_extruders=[0, 1],
            stat=0,
            info=2,
        )

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_status = MagicMock(return_value=state)
            mock_pm.is_awaiting_plate_clear = MagicMock(return_value=False)

            response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert response.status_code == 200
        result = response.json()
        assert result["fila_switch"] is not None
        assert result["fila_switch"]["installed"] is True
        assert result["fila_switch"]["in_slots"] == [-1, 2]
        assert result["fila_switch"]["out_extruders"] == [0, 1]
        assert result["fila_switch"]["stat"] == 0
        assert result["fila_switch"]["info"] == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_printer_status_omits_fila_switch_when_not_installed(
        self, async_client: AsyncClient, printer_factory
    ):
        """Without the FTS accessory, fila_switch must be null so the frontend
        keeps applying the per-extruder filter on regular dual-nozzle printers."""
        from backend.app.services.bambu_mqtt import PrinterState

        printer = await printer_factory()

        state = PrinterState()
        state.connected = True
        state.state = "IDLE"
        # default fila_switch — installed = False

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_status = MagicMock(return_value=state)
            mock_pm.is_awaiting_plate_clear = MagicMock(return_value=False)

            response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert response.status_code == 200
        result = response.json()
        assert result["fila_switch"] is None


class TestPrinterAccessCodeVisibility:
    """access_code (the printer's MQTT credential) must only reach callers holding
    PRINTERS_UPDATE (Admin / Operator JWTs). Viewers and read-scoped API keys pass
    PRINTERS_READ but must get the field redacted — otherwise a read-only caller
    could talk to the printer's MQTT directly and bypass RBAC (upstream
    8283b175 / 9a432f00)."""

    @pytest.fixture
    async def rbac_tokens(self, async_client: AsyncClient):
        """Mint operator (holds PRINTERS_UPDATE) + viewer (read-only) JWTs using
        the pre-seeded admin + default groups."""
        from backend.app.core.auth import create_access_token

        admin_token = create_access_token(data={"sub": "test_admin"})

        groups = (
            await async_client.get(
                "/api/v1/groups/",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        ).json()
        operators_group = next(g for g in groups if g["name"] == "Operators")
        viewers_group = next(g for g in groups if g["name"] == "Viewers")

        await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "username": "secret_operator",
                "password": "OperatorPass123!",
                "group_ids": [operators_group["id"]],
            },
        )
        operator_token = (
            await async_client.post(
                "/api/v1/auth/login",
                json={"username": "secret_operator", "password": "OperatorPass123!"},
            )
        ).json()["access_token"]

        await async_client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "username": "secret_viewer",
                "password": "ViewerPass123!",
                "group_ids": [viewers_group["id"]],
            },
        )
        viewer_token = (
            await async_client.post(
                "/api/v1/auth/login",
                json={"username": "secret_viewer", "password": "ViewerPass123!"},
            )
        ).json()["access_token"]

        return {
            "admin_token": admin_token,
            "operator_token": operator_token,
            "viewer_token": viewer_token,
        }

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_sees_access_code(self, async_client: AsyncClient, rbac_tokens, printer_factory, db_session):
        """Admin (PRINTERS_UPDATE via Administrators) sees access_code on list + detail."""
        printer = await printer_factory(name="Secret Printer", access_code="SECRET-CODE")
        headers = {"Authorization": f"Bearer {rbac_tokens['admin_token']}"}

        listing = await async_client.get("/api/v1/printers/", headers=headers)
        assert listing.status_code == 200
        row = next(p for p in listing.json() if p["id"] == printer.id)
        assert row["access_code"] == "SECRET-CODE"

        detail = await async_client.get(f"/api/v1/printers/{printer.id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["access_code"] == "SECRET-CODE"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_sees_access_code(self, async_client: AsyncClient, rbac_tokens, printer_factory, db_session):
        """Operator holds PRINTERS_UPDATE, so access_code is present on list + detail."""
        printer = await printer_factory(name="Secret Printer", access_code="SECRET-CODE")
        headers = {"Authorization": f"Bearer {rbac_tokens['operator_token']}"}

        listing = await async_client.get("/api/v1/printers/", headers=headers)
        assert listing.status_code == 200
        row = next(p for p in listing.json() if p["id"] == printer.id)
        assert row["access_code"] == "SECRET-CODE"

        detail = await async_client.get(f"/api/v1/printers/{printer.id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["access_code"] == "SECRET-CODE"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_viewer_cannot_see_access_code(
        self, async_client: AsyncClient, rbac_tokens, printer_factory, db_session
    ):
        """Viewer passes PRINTERS_READ but lacks PRINTERS_UPDATE — access_code must be
        redacted on list + detail, while non-secret fields stay visible."""
        printer = await printer_factory(name="Secret Printer", access_code="SECRET-CODE")
        headers = {"Authorization": f"Bearer {rbac_tokens['viewer_token']}"}

        listing = await async_client.get("/api/v1/printers/", headers=headers)
        assert listing.status_code == 200
        row = next(p for p in listing.json() if p["id"] == printer.id)
        assert row.get("access_code") is None
        # Non-secret fields still reach the viewer.
        assert row["name"] == "Secret Printer"
        assert row["serial_number"] == printer.serial_number

        detail = await async_client.get(f"/api/v1/printers/{printer.id}", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body.get("access_code") is None
        assert body["name"] == "Secret Printer"
        assert body["serial_number"] == printer.serial_number

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_read_scoped_api_key_cannot_see_access_code(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        """A read-scoped API key (can_read_status) passes PRINTERS_READ but resolves to
        no user, so it can never hold PRINTERS_UPDATE — access_code must be redacted on
        list + detail. The inherited admin Authorization header is cleared so the request
        is genuinely API-key-only."""
        from backend.app.core.auth import create_access_token, generate_api_key
        from backend.app.core.database import async_session
        from backend.app.models.api_key import APIKey

        printer = await printer_factory(name="Secret Printer", access_code="SECRET-CODE")

        raw_key, key_hash, key_prefix = generate_api_key()
        async with async_session() as db:
            db.add(
                APIKey(
                    name="read-only-key",
                    key_hash=key_hash,
                    key_prefix=key_prefix,
                    can_read_status=True,
                )
            )
            await db.commit()

        # Drop the fixture's admin JWT so the request authenticates ONLY via the key.
        del async_client.headers["Authorization"]
        try:
            listing = await async_client.get("/api/v1/printers/", headers={"X-API-Key": raw_key})
            detail = await async_client.get(f"/api/v1/printers/{printer.id}", headers={"X-API-Key": raw_key})
        finally:
            async_client.headers["Authorization"] = f"Bearer {create_access_token(data={'sub': 'test_admin'})}"

        assert listing.status_code == 200
        row = next(p for p in listing.json() if p["id"] == printer.id)
        assert row.get("access_code") is None
        assert row["name"] == "Secret Printer"
        assert row["serial_number"] == printer.serial_number

        assert detail.status_code == 200
        body = detail.json()
        assert body.get("access_code") is None
        assert body["name"] == "Secret Printer"


class TestExecuteHMSActionAPI:
    """The /hms/execute-action route decides ack by probing `_last_message_time`
    (bumped on every inbound MQTT push) rather than diffing (gcode_state,
    hms_errors-len). This survives the wrong-plate IGNORE_RESUME re-pause (#1869)
    where both state fields round-trip to their pre-publish values inside the
    ack window even though the firmware fully ack'd.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_execute_hms_action_success(self, async_client: AsyncClient, printer_factory):
        """200 when the dispatcher returns True AND the printer pushes at least one
        MQTT message into the ack-wait window."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.state.state = "PAUSE"
        mock_client.state.hms_errors = [object()]
        mock_client._last_message_time = 100.0

        def _act(*_a, **_kw):
            # The pushall that follows every command produces a fresh inbound push;
            # the state fields themselves don't have to move (#1869).
            mock_client._last_message_time = 100.5
            return True

        mock_client.execute_hms_action.side_effect = _act

        with (
            patch("backend.app.api.routes.printers.printer_manager") as mock_pm,
            _orig_patch("backend.app.api.routes.printers.HMS_ACTION_ACK_WAIT_SECONDS", 0.01),
        ):
            mock_pm.get_client.return_value = mock_client

            body = {"print_error": "05008051", "action": "IGNORE_RESUME", "job_id": None}
            response = await async_client.post(f"/api/v1/printers/{printer.id}/hms/execute-action", json=body)

            assert response.status_code == 200
            assert response.json()["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_execute_hms_action_no_printer_ack_returns_502(self, async_client: AsyncClient, printer_factory):
        """502 when publish succeeded but no MQTT message arrives back within the
        ack-wait window — the firmware-silent-drop failure mode #1830 surfaces."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.state.state = "PAUSE"
        mock_client.state.hms_errors = [object()]
        mock_client._last_message_time = 100.0
        mock_client.execute_hms_action.return_value = True  # publish "succeeded"
        # Crucially: _last_message_time does NOT advance → no inbound push.

        with (
            patch("backend.app.api.routes.printers.printer_manager") as mock_pm,
            _orig_patch("backend.app.api.routes.printers.HMS_ACTION_ACK_WAIT_SECONDS", 0.01),
        ):
            mock_pm.get_client.return_value = mock_client

            body = {"print_error": "05008051", "action": "IGNORE_RESUME", "job_id": None}
            response = await async_client.post(f"/api/v1/printers/{printer.id}/hms/execute-action", json=body)

            assert response.status_code == 502
            assert "acknowledge" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_execute_hms_action_ignore_resume_repauses_within_window_still_acks(
        self, async_client: AsyncClient, printer_factory
    ):
        """200 when the printer ack'd but immediately re-paused with the same fault —
        wrong-plate IGNORE_RESUME (#1869). The old (gcode_state, hms_errors-len) diff
        produced a false 502 because both fields round-tripped inside the ack window.
        Probing `_last_message_time` survives the round-trip."""
        printer = await printer_factory(name="Test Printer")

        mock_client = MagicMock()
        mock_client.state.state = "PAUSE"
        mock_client.state.hms_errors = [object()]
        mock_client._last_message_time = 100.0

        def _act(*_a, **_kw):
            # Ack'd, briefly resumed, re-detected the wrong plate, re-paused. Net diff
            # on state fields is zero, but a fresh status push DID arrive.
            mock_client._last_message_time = 100.4
            mock_client.state.state = "PAUSE"  # round-tripped
            mock_client.state.hms_errors = [object()]  # same length
            return True

        mock_client.execute_hms_action.side_effect = _act

        with (
            patch("backend.app.api.routes.printers.printer_manager") as mock_pm,
            _orig_patch("backend.app.api.routes.printers.HMS_ACTION_ACK_WAIT_SECONDS", 0.01),
        ):
            mock_pm.get_client.return_value = mock_client

            body = {"print_error": "05008051", "action": "IGNORE_RESUME", "job_id": None}
            response = await async_client.post(f"/api/v1/printers/{printer.id}/hms/execute-action", json=body)

            assert response.status_code == 200
            assert response.json()["success"] is True
