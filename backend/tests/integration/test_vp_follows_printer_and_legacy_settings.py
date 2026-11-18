"""A printer's new address reaches its proxy Virtual Printers; the legacy VP settings route is not
blocked by values it stored long ago (upstream Bambuddy c23c073dd, b9b62f739).
"""

import inspect
from unittest.mock import AsyncMock

import pytest

from backend.app.api.routes import settings
from backend.app.services.virtual_printer import manager as vp_manager, virtual_printer_manager

pytestmark = pytest.mark.integration


async def _seed(db, values):
    for key, value in values.items():
        await settings.set_setting(db, key, str(value).lower() if isinstance(value, bool) else str(value))
    await db.commit()


class TestPrinterAddress:
    async def test_a_new_address_syncs_the_virtual_printers(self, async_client, printer_factory, monkeypatch):
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory(ip_address="192.168.1.50", is_active=False)
        sync = AsyncMock()
        monkeypatch.setattr(virtual_printer_manager, "sync_from_db", sync)
        monkeypatch.setattr(printer_manager, "disconnect_printer", lambda pid: None)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"ip_address": "192.168.1.77"})

        assert response.status_code == 200, response.text
        assert sync.await_count == 1

    async def test_an_edit_that_keeps_the_address_syncs_nothing(self, async_client, printer_factory, monkeypatch):
        printer = await printer_factory(ip_address="192.168.1.50", is_active=False)
        sync = AsyncMock()
        monkeypatch.setattr(virtual_printer_manager, "sync_from_db", sync)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"name": "Renamed"})

        assert response.status_code == 200, response.text
        assert sync.await_count == 0

    async def test_a_failed_sync_does_not_fail_the_saved_edit(self, async_client, printer_factory, monkeypatch):
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory(ip_address="192.168.1.50", is_active=False)
        monkeypatch.setattr(virtual_printer_manager, "sync_from_db", AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(printer_manager, "disconnect_printer", lambda pid: None)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"ip_address": "192.168.1.78"})

        assert response.status_code == 200, response.text
        assert response.json()["ip_address"] == "192.168.1.78"


def test_a_proxy_is_built_with_every_field_the_change_check_compares():
    """A field the check compares and the proxy constructor leaves out restarts that VP on every sync."""
    source = inspect.getsource(vp_manager.VirtualPrinterManager._sync_from_db_locked)
    proxy_block = source.split("start_proxy()")[0].rsplit("VirtualPrinterInstance(", 1)[1]

    for field in ("target_printer_id", "target_folder_id", "auto_dispatch", "save_ams_mapping", "gcode_injection"):
        assert f"{field}=vp.{field}" in proxy_block, field


class TestLegacySettingsRoute:
    async def test_a_stale_stored_mode_does_not_block_the_archive_name_setting(
        self, async_client, db_session, monkeypatch
    ):
        monkeypatch.setattr(virtual_printer_manager, "configure", AsyncMock())
        await _seed(
            db_session,
            {
                "virtual_printer_mode": "immediate",
                "virtual_printer_enabled": True,
                "virtual_printer_access_code": "12345678",
            },
        )

        response = await async_client.put("/api/v1/settings/virtual-printer", json={"archive_name_source": "filename"})

        assert response.status_code == 200, response.text
        assert await settings.get_setting(db_session, "virtual_printer_archive_name_source") == "filename"
        # What the request did not speak of is left exactly as it was.
        assert await settings.get_setting(db_session, "virtual_printer_mode") == "immediate"
        assert await settings.get_setting(db_session, "virtual_printer_enabled") == "true"

    async def test_a_deleted_stored_target_does_not_block_it_either(self, async_client, db_session, monkeypatch):
        monkeypatch.setattr(virtual_printer_manager, "configure", AsyncMock())
        await _seed(db_session, {"virtual_printer_mode": "proxy", "virtual_printer_target_printer_id": 99999})

        response = await async_client.put("/api/v1/settings/virtual-printer", json={"archive_name_source": "metadata"})

        assert response.status_code == 200, response.text

    async def test_a_request_that_names_a_bad_mode_is_still_refused(self, async_client, monkeypatch):
        monkeypatch.setattr(virtual_printer_manager, "configure", AsyncMock())

        response = await async_client.put("/api/v1/settings/virtual-printer", json={"mode": "immediate"})

        assert response.status_code == 400
