"""Integration tests for System API endpoints.

Tests the full request/response cycle for /api/v1/system/ endpoints.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient


class TestSystemAPI:
    """Integration tests for /api/v1/system/ endpoints."""

    # ========================================================================
    # System Info Endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_system_info(self, async_client: AsyncClient):
        """Verify system info endpoint returns expected structure."""
        # Mock psutil to avoid system-specific values
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        assert response.status_code == 200
        result = response.json()

        # Verify top-level structure
        assert "app" in result
        assert "database" in result
        assert "printers" in result
        assert "storage" in result
        assert "system" in result
        assert "memory" in result
        assert "cpu" in result

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_app_section(self, async_client: AsyncClient):
        """Verify app section contains version and directory info."""
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        app_info = result["app"]

        assert "version" in app_info
        assert "base_dir" in app_info
        assert "archive_dir" in app_info

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_database_section(self, async_client: AsyncClient):
        """Verify database section contains counts and statistics."""
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        db_info = result["database"]

        assert "archives" in db_info
        assert "archives_completed" in db_info
        assert "archives_failed" in db_info
        assert "printers" in db_info
        # Key is `filaments` (matches the user-visible "Filaments / Філаменти"
        # label on the Information page + the frontend SystemInfo type). The
        # underlying DB table / model is `Spool`, but the API surface here
        # speaks the same language as the UI.
        assert "filaments" in db_info
        assert "projects" in db_info
        assert "smart_plugs" in db_info
        assert "total_print_time_seconds" in db_info
        assert "total_print_time_formatted" in db_info
        assert "total_filament_grams" in db_info
        assert "total_filament_kg" in db_info

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_storage_section(self, async_client: AsyncClient):
        """Verify storage section contains disk usage info."""
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        storage_info = result["storage"]

        assert "archive_size_bytes" in storage_info
        assert "archive_size_formatted" in storage_info
        assert "database_size_bytes" in storage_info
        assert "database_size_formatted" in storage_info
        assert "disk_total_bytes" in storage_info
        assert "disk_total_formatted" in storage_info
        assert "disk_used_bytes" in storage_info
        assert "disk_free_bytes" in storage_info
        assert "disk_percent_used" in storage_info

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_memory_section(self, async_client: AsyncClient):
        """Verify memory section contains RAM usage info."""
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        memory_info = result["memory"]

        assert "total_bytes" in memory_info
        assert "total_formatted" in memory_info
        assert "available_bytes" in memory_info
        assert "used_bytes" in memory_info
        assert "percent_used" in memory_info

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_cpu_section(self, async_client: AsyncClient):
        """Verify CPU section contains processor info."""
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        cpu_info = result["cpu"]

        assert "count" in cpu_info
        assert "count_logical" in cpu_info
        assert "percent" in cpu_info

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_printers_section(self, async_client: AsyncClient, printer_factory):
        """Verify printers section contains connected printer info."""
        # Create a test printer
        _printer = await printer_factory(name="Test Printer", model="X1C")

        with (
            patch("backend.app.api.routes.system.psutil") as mock_psutil,
            patch("backend.app.api.routes.system.printer_manager") as mock_pm,
        ):
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            # Mock no connected printers for simplicity
            mock_pm._clients = {}

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        printers_info = result["printers"]

        assert "total" in printers_info
        assert "connected" in printers_info
        assert "connected_list" in printers_info
        assert printers_info["total"] >= 1  # At least our test printer

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_system_info_with_archives(self, async_client: AsyncClient, printer_factory, archive_factory):
        """Verify database stats include archive counts."""
        printer = await printer_factory()
        await archive_factory(printer.id, status="completed", print_time_seconds=3600)
        await archive_factory(printer.id, status="failed", print_time_seconds=1800)

        with (
            patch("backend.app.api.routes.system.psutil") as mock_psutil,
            patch("backend.app.api.routes.system.printer_manager") as mock_pm,
        ):
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0
            mock_pm._clients = {}

            response = await async_client.get("/api/v1/system/info")

        result = response.json()
        db_info = result["database"]

        assert db_info["archives"] >= 2
        assert db_info["archives_completed"] >= 1
        assert db_info["archives_failed"] >= 1
        assert db_info["total_print_time_seconds"] >= 5400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_boot_time_uses_pid1_create_time(self, async_client: AsyncClient):
        """#1690: container installs (Docker/LXC) share the host kernel, so
        psutil.boot_time() returns the host's boot time instead of the
        container's. Reading PID 1's create_time gives the container start
        time on containers and matches host boot on bare metal."""
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            # Host boot is FOUR DAYS earlier than the container's PID 1 start.
            # The route must report the PID 1 value, not the host value.
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700345600.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        assert response.status_code == 200
        result = response.json()
        # The route emits tz-aware UTC (#1690 follow-up) so isoformat() carries a
        # "+00:00" marker and the frontend doesn't double-convert. Compute the
        # expected value the same way.
        from datetime import datetime as _dt, timezone as _tz

        assert (
            result["system"]["boot_time"] == _dt.fromtimestamp(1700345600.0, tz=_tz.utc).isoformat()
        )  # PID 1 value, not host boot
        # PID 1 was queried with pid=1 (not the worker pid).
        mock_psutil.Process.assert_called_with(1)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_boot_time_falls_back_to_psutil_boot_time_on_pid1_failure(self, async_client: AsyncClient):
        """If PID 1 is unreadable (rare — locked-down container, /proc not
        mounted), fall back to psutil.boot_time() so the endpoint still
        returns 200 with the best available answer."""
        import psutil as real_psutil

        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            # Use the real exception classes so the route's except clause matches.
            mock_psutil.Error = real_psutil.Error
            mock_psutil.Process.side_effect = real_psutil.NoSuchProcess(1)
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        assert response.status_code == 200
        result = response.json()
        from datetime import datetime as _dt, timezone as _tz

        assert (
            result["system"]["boot_time"] == _dt.fromtimestamp(1700000000.0, tz=_tz.utc).isoformat()
        )  # fell back to host boot_time

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_boot_time_isoformat_carries_utc_marker(self, async_client: AsyncClient):
        """#1690 follow-up: the boot_time string must include a UTC tz marker.

        Without it the frontend's parseUTCDate(...) appends 'Z' to a naive-
        local-time string, treats it as UTC, and converts to local — applying
        the local offset twice. The reporter (UTC+3) saw boot_time +3h ahead
        even though uptime was correct (uptime is computed backend-side from
        two naive-local values whose delta is right). The fix makes both ends
        tz-aware UTC and emits an explicit offset.
        """
        with patch("backend.app.api.routes.system.psutil") as mock_psutil:
            mock_psutil.disk_usage.return_value = MagicMock(
                total=500000000000, used=250000000000, free=250000000000, percent=50.0
            )
            mock_psutil.virtual_memory.return_value = MagicMock(
                total=16000000000, available=8000000000, used=8000000000, percent=50.0
            )
            mock_psutil.boot_time.return_value = 1700000000.0
            mock_psutil.Process.return_value.create_time.return_value = 1700345600.0
            mock_psutil.cpu_count.return_value = 4
            mock_psutil.cpu_percent.return_value = 25.0

            response = await async_client.get("/api/v1/system/info")

        assert response.status_code == 200
        boot_time = response.json()["system"]["boot_time"]
        assert boot_time.endswith("+00:00") or boot_time.endswith("Z"), (
            f"boot_time {boot_time!r} must carry a UTC tz marker; without one the "
            "frontend double-converts via parseUTCDate"
        )


class TestSystemHelperFunctions:
    """Tests for system info helper functions."""

    def test_format_bytes_bytes(self):
        """Verify format_bytes handles bytes correctly."""
        from backend.app.api.routes.system import format_bytes

        assert format_bytes(500) == "500.0 B"

    def test_format_bytes_kilobytes(self):
        """Verify format_bytes handles kilobytes correctly."""
        from backend.app.api.routes.system import format_bytes

        result = format_bytes(1536)
        assert "KB" in result

    def test_format_bytes_megabytes(self):
        """Verify format_bytes handles megabytes correctly."""
        from backend.app.api.routes.system import format_bytes

        result = format_bytes(1536 * 1024)
        assert "MB" in result

    def test_format_bytes_gigabytes(self):
        """Verify format_bytes handles gigabytes correctly."""
        from backend.app.api.routes.system import format_bytes

        result = format_bytes(1536 * 1024 * 1024)
        assert "GB" in result

    def test_format_uptime_minutes(self):
        """Verify format_uptime handles minutes correctly."""
        from backend.app.api.routes.system import format_uptime

        result = format_uptime(300)  # 5 minutes
        assert "5m" in result

    def test_format_uptime_hours(self):
        """Verify format_uptime handles hours correctly."""
        from backend.app.api.routes.system import format_uptime

        result = format_uptime(7200)  # 2 hours
        assert "2h" in result

    def test_format_uptime_days(self):
        """Verify format_uptime handles days correctly."""
        from backend.app.api.routes.system import format_uptime

        result = format_uptime(86400 * 2 + 3600 * 5)  # 2 days 5 hours
        assert "2d" in result
        assert "5h" in result

    def test_format_uptime_less_than_minute(self):
        """Verify format_uptime handles < 1 minute correctly."""
        from backend.app.api.routes.system import format_uptime

        result = format_uptime(30)  # 30 seconds
        assert result == "< 1m"
