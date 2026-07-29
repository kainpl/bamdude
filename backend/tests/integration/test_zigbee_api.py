"""Coordinator status and serial-port discovery.

No pairing, no control — those are phases 2 and 3. What these pin down is that
neither endpoint can become a way to take the page down: a machine with no
serial ports, or a driver that makes enumeration raise, must both answer 200.
"""

import pytest
from httpx import AsyncClient


class TestZigbeeStatus:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reports_disabled_by_default(self, async_client: AsyncClient):
        """Off unless deliberately turned on."""
        resp = await async_client.get("/api/v1/zigbee/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "disabled"
        assert body["reason"] == ""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_surfaces_the_reason_when_it_failed(self, async_client: AsyncClient, monkeypatch):
        """The reason is the entire explanation until the phase-4 UI exists."""
        from backend.app.api.routes import zigbee as zigbee_routes
        from backend.app.services.zigbee.coordinator import CoordinatorState, CoordinatorStatus

        monkeypatch.setattr(
            zigbee_routes.zigbee_coordinator,
            "_status",
            CoordinatorStatus(CoordinatorState.ERROR, "no such device"),
            raising=False,
        )
        resp = await async_client.get("/api/v1/zigbee/status")

        assert resp.status_code == 200
        assert resp.json() == {"state": "error", "reason": "no such device"}


class TestZigbeePorts:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lists_ports(self, async_client: AsyncClient):
        resp = await async_client.get("/api/v1/zigbee/ports")

        assert resp.status_code == 200
        assert isinstance(resp.json()["ports"], list)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_ports_is_an_empty_list_not_an_error(self, async_client: AsyncClient, monkeypatch):
        """A machine with no serial ports is normal, not a failure."""
        from backend.app.api.routes import zigbee as zigbee_routes

        monkeypatch.setattr(zigbee_routes, "_comports", lambda: [])
        resp = await async_client.get("/api/v1/zigbee/ports")

        assert resp.status_code == 200
        assert resp.json()["ports"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_enumeration_failure_does_not_500(self, async_client: AsyncClient, monkeypatch):
        """A flaky driver must cost the port list, not the settings page."""
        from backend.app.api.routes import zigbee as zigbee_routes

        def _boom():
            raise OSError("serial driver exploded")

        monkeypatch.setattr(zigbee_routes, "_comports", _boom)
        resp = await async_client.get("/api/v1/zigbee/ports")

        assert resp.status_code == 200
        assert resp.json()["ports"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_port_entries_carry_what_the_ui_needs(self, async_client: AsyncClient, monkeypatch):
        """device is what gets saved; description is what the user recognises."""
        from types import SimpleNamespace

        from backend.app.api.routes import zigbee as zigbee_routes

        monkeypatch.setattr(
            zigbee_routes,
            "_comports",
            lambda: [SimpleNamespace(device="COM7", description="SONOFF Zigbee Dongle", hwid="USB\\VID_1A86")],
        )
        resp = await async_client.get("/api/v1/zigbee/ports")

        assert resp.json()["ports"] == [
            {"device": "COM7", "description": "SONOFF Zigbee Dongle", "hwid": "USB\\VID_1A86"}
        ]


class TestZigbeeSettingsAreReachable:
    """The three coordinator settings must be writable through the normal API.

    ``update_settings`` persists exactly the fields ``AppSettingsUpdate``
    declares, and Pydantic drops anything undeclared *silently*. Without these
    fields the settings appear to save and simply do not — leaving no way to
    configure Zigbee short of writing to the database by hand.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_settings_round_trip(self, async_client: AsyncClient):
        resp = await async_client.patch(
            "/api/v1/settings",
            json={"zigbee_enabled": True, "zigbee_transport": "usb", "zigbee_path": "COM7"},
        )
        assert resp.status_code == 200

        current = await async_client.get("/api/v1/settings")
        body = current.json()
        assert body["zigbee_enabled"] is True
        assert body["zigbee_transport"] == "usb"
        assert body["zigbee_path"] == "COM7"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_defaults_are_off(self, async_client: AsyncClient):
        body = (await async_client.get("/api/v1/settings")).json()
        assert body["zigbee_enabled"] is False
        assert body["zigbee_transport"] == "ethernet"


def _up(monkeypatch, app):
    """Put the module-level coordinator into a usable state for one test."""
    from backend.app.api.routes import zigbee as zigbee_routes
    from backend.app.services.zigbee.coordinator import CoordinatorState, CoordinatorStatus

    monkeypatch.setattr(zigbee_routes.zigbee_coordinator, "_app", app, raising=False)
    monkeypatch.setattr(
        zigbee_routes.zigbee_coordinator, "_status", CoordinatorStatus(CoordinatorState.UP), raising=False
    )


class TestPermitJoin:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_refuses_while_the_coordinator_is_down(self, async_client: AsyncClient):
        """Opening a window against a dead radio would 'succeed' and then do
        nothing for a minute, which is worse than refusing."""
        resp = await async_client.post("/api/v1/zigbee/permit", json={"seconds": 60})

        assert resp.status_code == 409
        assert resp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("seconds", [0, 255, 3600, -1])
    async def test_rejects_out_of_range_windows(self, async_client: AsyncClient, seconds):
        """255 means 'permanently open' in the Zigbee spec — not somewhere a UI
        should be able to land by rounding up."""
        resp = await async_client.post("/api/v1/zigbee/permit", json={"seconds": seconds})

        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_opens_the_window_when_up(self, async_client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock

        app = AsyncMock()
        _up(monkeypatch, app)

        resp = await async_client.post("/api/v1/zigbee/permit", json={"seconds": 30})

        assert resp.status_code == 200
        app.permit.assert_awaited_once_with(time_s=30)


class TestDeviceList:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_empty_when_the_coordinator_is_down(self, async_client: AsyncClient):
        """Down is not an error here — there are simply no devices to list."""
        resp = await async_client.get("/api/v1/zigbee/devices")

        assert resp.status_code == 200
        assert resp.json()["devices"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lists_paired_devices_with_capabilities(self, async_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF

        device = SimpleNamespace(
            ieee="34:8d:13:ff:fe:11:e4:6f",
            nwk=0x1234,
            manufacturer="SONOFF",
            model="S60ZBTPF",
            endpoints={1: SimpleNamespace(in_clusters={ON_OFF: 1, METERING: 1, ELECTRICAL_MEASUREMENT: 1})},
        )
        app = AsyncMock()
        app.devices = {device.ieee: device}
        _up(monkeypatch, app)

        resp = await async_client.get("/api/v1/zigbee/devices")

        body = resp.json()["devices"]
        assert len(body) == 1
        assert body[0]["model"] == "S60ZBTPF"
        assert body[0]["has_metering"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_unknown_ieee_is_404(self, async_client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock

        app = AsyncMock()
        app.devices = {}
        _up(monkeypatch, app)

        resp = await async_client.delete("/api/v1/zigbee/devices/00:11:22:33:44:55:66:77")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_matches_ieee_case_insensitively(self, async_client: AsyncClient, monkeypatch):
        """zigpy stringifies EUI64 lower-case; a UI may echo whatever was typed."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        device = SimpleNamespace(ieee="34:8d:13:ff:fe:11:e4:6f", nwk=1, manufacturer=None, model=None, endpoints={})
        app = AsyncMock()
        app.devices = {device.ieee: device}
        _up(monkeypatch, app)

        resp = await async_client.delete("/api/v1/zigbee/devices/34:8D:13:FF:FE:11:E4:6F")

        assert resp.status_code == 200
        app.remove.assert_awaited_once()
