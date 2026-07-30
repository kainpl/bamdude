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
        # Field-wise, not whole-payload equality: this test is about the reason
        # surfacing, and /status is expected to grow fields (it already gained
        # the radio identity). An exact-match assertion here would fail on every
        # future addition while telling nobody anything about the reason.
        body = resp.json()
        assert body["state"] == "error"
        assert body["reason"] == "no such device"


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


class TestCoordinatorIsNotADevice:
    """Found on real hardware: GET /devices listed the radio itself.

    zigpy keeps the coordinator in its own device table and the Dongle-M reports
    an On/Off cluster, so it arrived looking exactly like a pairable plug.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_radio_is_not_listed(self, async_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.devices import ON_OFF

        radio = SimpleNamespace(
            ieee="34:8d:13:ff:fe:11:e4:6f",
            nwk=0x0000,
            manufacturer="Silicon Labs",
            model="EZSP",
            endpoints={1: SimpleNamespace(in_clusters={ON_OFF: 1})},
        )
        plug = SimpleNamespace(
            ieee="a4:c1:38:0b:5a:9c:ff:ff",
            nwk=0xF6B4,
            manufacturer="SONOFF",
            model="S60ZBTPF",
            endpoints={1: SimpleNamespace(in_clusters={ON_OFF: 1})},
        )
        app = AsyncMock()
        app.devices = {radio.ieee: radio, plug.ieee: plug}
        _up(monkeypatch, app)

        devices = (await async_client.get("/api/v1/zigbee/devices")).json()["devices"]

        assert [d["model"] for d in devices] == ["S60ZBTPF"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_radio_cannot_be_deleted(self, async_client: AsyncClient, monkeypatch):
        """remove() on our own radio would take the network with it."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        radio = SimpleNamespace(
            ieee="34:8d:13:ff:fe:11:e4:6f", nwk=0x0000, manufacturer=None, model="EZSP", endpoints={}
        )
        app = AsyncMock()
        app.devices = {radio.ieee: radio}
        _up(monkeypatch, app)

        resp = await async_client.delete("/api/v1/zigbee/devices/34:8d:13:ff:fe:11:e4:6f")

        assert resp.status_code == 404
        app.remove.assert_not_awaited()


class TestCoordinatorIdentityInStatus:
    """The radio's identity belongs with its status, not in the device list.

    /devices answers "what can I manage"; the coordinator is not one of those.
    But its identity is real diagnostic value — which dongle am I actually
    talking to, and on which channel — so it lives here instead of nowhere.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_carries_the_radio_identity_when_up(self, async_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        app = AsyncMock()
        app.state = SimpleNamespace(
            node_info=SimpleNamespace(
                ieee="34:8d:13:ff:fe:11:e4:6f",
                nwk=0x0000,
                model="EZSP",
                manufacturer="Silicon Labs",
                version="7.4.5",
            ),
            network_info=SimpleNamespace(channel=25, pan_id=0x1A2B, network_key="SECRET-DO-NOT-LEAK"),
        )
        _up(monkeypatch, app)

        body = (await async_client.get("/api/v1/zigbee/status")).json()

        assert body["coordinator"]["ieee"] == "34:8d:13:ff:fe:11:e4:6f"
        assert body["coordinator"]["model"] == "EZSP"
        assert body["network"]["channel"] == 25

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_network_key_is_never_serialised(self, async_client: AsyncClient, monkeypatch):
        """Losing it means re-pairing every device; leaking it is worse."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        app = AsyncMock()
        app.state = SimpleNamespace(
            node_info=SimpleNamespace(ieee="a", nwk=0, model="m", manufacturer="x", version="1"),
            network_info=SimpleNamespace(channel=25, pan_id=1, network_key="SECRET-DO-NOT-LEAK"),
        )
        _up(monkeypatch, app)

        raw = (await async_client.get("/api/v1/zigbee/status")).text

        assert "SECRET-DO-NOT-LEAK" not in raw
        assert "network_key" not in raw

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_status_omits_identity_when_the_radio_is_down(self, async_client: AsyncClient):
        body = (await async_client.get("/api/v1/zigbee/status")).json()

        assert body["state"] == "disabled"
        assert body["coordinator"] is None
        assert body["network"] is None


class TestBindingAPlugToAPrinter:
    """Turning a paired device into a working plug.

    Phase 2 deliberately created no rows; this is where they appear. The
    validation matters more than it looks: an IEEE that is not on the mesh, or
    the coordinator's own address, would both produce a plug row that can never
    switch anything — and the operator would see a broken plug rather than a
    rejected request.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_binding_a_paired_plug_succeeds(self, async_client: AsyncClient, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.devices import METERING, ON_OFF

        plug = SimpleNamespace(
            ieee="a4:c1:38:0b:5a:9c:ff:ff",
            nwk=0xF6B4,
            manufacturer="SONOFF",
            model="S60ZBTPF",
            endpoints={1: SimpleNamespace(in_clusters={ON_OFF: 1, METERING: 1})},
        )
        app = AsyncMock()
        app.devices = {plug.ieee: plug}
        _up(monkeypatch, app)

        resp = await async_client.post(
            "/api/v1/smart-plugs/",
            json={"name": "Printer 1 power", "plug_type": "zigbee", "zigbee_ieee": plug.ieee},
        )

        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["zigbee_ieee"] == plug.ieee

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_ieee_that_is_not_paired_is_refused(self, async_client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock

        app = AsyncMock()
        app.devices = {}
        _up(monkeypatch, app)

        resp = await async_client.post(
            "/api/v1/smart-plugs/",
            json={"name": "ghost", "plug_type": "zigbee", "zigbee_ieee": "00:11:22:33:44:55:66:77"},
        )

        assert resp.status_code == 400
        assert "00:11:22:33:44:55:66:77" in resp.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_coordinator_cannot_be_bound(self, async_client: AsyncClient, monkeypatch):
        """Phase 2 stopped the radio appearing in the device list; this is the
        endpoint that would otherwise still let someone bind it."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.devices import ON_OFF

        radio = SimpleNamespace(
            ieee="34:8d:13:ff:fe:11:e4:6f",
            nwk=0x0000,
            manufacturer="Silicon Labs",
            model="EZSP",
            endpoints={1: SimpleNamespace(in_clusters={ON_OFF: 1})},
        )
        app = AsyncMock()
        app.devices = {radio.ieee: radio}
        _up(monkeypatch, app)

        resp = await async_client.post(
            "/api/v1/smart-plugs/",
            json={"name": "the dongle", "plug_type": "zigbee", "zigbee_ieee": radio.ieee},
        )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zigbee_without_an_ieee_is_refused(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/smart-plugs/", json={"name": "nowhere", "plug_type": "zigbee"})

        assert resp.status_code in (400, 422)


class TestAttributeDump:
    """The diagnostic that made a silent quirk failure visible.

    Kept in the code rather than deleted after the debugging session: it shows
    the device's raw answer next to the value the cluster cache holds, and those
    two diverge exactly where a quirk intervened. Without that comparison there
    is no way to tell a quirk that is working from one that silently did not
    match — which cost a full hardware session to learn once.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_device_is_404(self, async_client: AsyncClient, monkeypatch):
        from unittest.mock import AsyncMock

        app = AsyncMock()
        app.devices = {}
        _up(monkeypatch, app)

        resp = await async_client.get("/api/v1/zigbee/devices/aa:bb:cc:dd:ee:ff:00:11/attributes")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_raw_and_cached_are_reported_separately(self, async_client: AsyncClient, monkeypatch):
        """A quirk that swallows a reading leaves the raw answer intact and the
        cache holding something else. Collapsing the two would hide it."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, ON_OFF

        em = SimpleNamespace(
            cluster_id=ELECTRICAL_MEASUREMENT,
            read_attributes=AsyncMock(return_value=({"active_power": 33}, {})),
            get=lambda name, default=None: {"active_power": 0}.get(name, default),
        )
        onoff = SimpleNamespace(
            cluster_id=ON_OFF,
            read_attributes=AsyncMock(return_value=({"on_off": 0}, {})),
            get=lambda name, default=None: {"on_off": 0}.get(name, default),
        )
        device = SimpleNamespace(
            ieee="a4:c1:38:0b:5a:9c:ff:ff",
            nwk=0x1234,
            manufacturer="SONOFF",
            model="S60ZBTPF",
            firmware_version=None,
            endpoints={1: SimpleNamespace(in_clusters={ON_OFF: onoff, ELECTRICAL_MEASUREMENT: em})},
        )
        app = AsyncMock()
        app.devices = {device.ieee: device}
        _up(monkeypatch, app)

        resp = await async_client.get(f"/api/v1/zigbee/devices/{device.ieee}/attributes")

        assert resp.status_code == 200
        body = resp.json()
        assert body["device_class"] == "SimpleNamespace"
        power = body["attributes"]["ep1/0x0B04"]["active_power"]
        assert "33" in power["raw"]
        assert "0" in power["cached"]


class TestRestart:
    """Restarting the radio from the UI.

    The step that must not be skipped is re-subscribing the plugs. After a restart
    ``zigbee_coordinator.app`` is a new object with new cluster objects. The driver
    resolves the device from the live app on every call, so commands and polling
    keep working — but cached listeners are still attached to the OLD clusters, so
    unsolicited reports quietly stop. Commands work, readings look current because
    polling refreshes them: exactly the half-configured state that cost this
    project a hardware session.

    These tests do not use the `_up` helper the way the others do. It patches
    `_status` directly, and this endpoint genuinely calls `stop()` then `start()`,
    which overwrite it — so the state has to come from a patched `start`, not from
    a patched attribute.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_disabled_answers_disabled_not_error(self, async_client: AsyncClient):
        """Zigbee off is a correct configuration, not a failure. This endpoint is
        also how the coordinator gets stopped after the box is unticked."""
        resp = await async_client.post("/api/v1/zigbee/restart")

        assert resp.status_code == 200
        assert resp.json()["state"] == "disabled"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stale_listeners_are_dropped(self, async_client: AsyncClient):
        from backend.app.services.zigbee.driver import zigbee_smart_plug_service

        zigbee_smart_plug_service._listeners[(1, 0x0006)] = object()

        await async_client.post("/api/v1/zigbee/restart")

        assert zigbee_smart_plug_service._listeners == {}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_failed_start_still_answers(self, async_client: AsyncClient, monkeypatch):
        """The coordinator never raises into startup by contract, and this endpoint
        keeps that promise: a dead dongle answers with a reason, not a 500."""
        from backend.app.services.zigbee.coordinator import (
            CoordinatorState,
            CoordinatorStatus,
            zigbee_coordinator as coord,
        )

        async def _fail(_settings):
            coord._status = CoordinatorStatus(CoordinatorState.ERROR, "dongle unplugged")

        monkeypatch.setattr(coord, "start", _fail)

        resp = await async_client.post("/api/v1/zigbee/restart")

        assert resp.status_code == 200
        assert resp.json()["state"] == "error"
        assert "dongle unplugged" in resp.json()["reason"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_plugs_are_resubscribed_once_the_radio_is_up(
        self, async_client: AsyncClient, monkeypatch, smart_plug_factory
    ):
        """The whole reason this endpoint is more than stop-then-start."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.services.zigbee import reporting
        from backend.app.services.zigbee.coordinator import (
            CoordinatorState,
            CoordinatorStatus,
            zigbee_coordinator as coord,
        )

        await smart_plug_factory(name="zb", plug_type="zigbee", ip_address=None, zigbee_ieee="a4:c1:38:0b:5a:9c:ff:ff")

        # Real values, not an AsyncMock: the response serialises the radio
        # identity, and FastAPI's encoder recurses forever into a Mock's
        # auto-generated attributes.
        fake_app = SimpleNamespace(
            devices={},
            state=SimpleNamespace(
                node_info=SimpleNamespace(
                    ieee="34:8d:13:ff:fe:11:e4:6f", nwk=0, model="EZSP", manufacturer="Silicon Labs", version="8.0.2"
                ),
                network_info=SimpleNamespace(channel=15, pan_id=6754),
            ),
        )

        async def _succeed(_settings):
            coord._status = CoordinatorStatus(CoordinatorState.UP)
            coord._app = fake_app

        monkeypatch.setattr(coord, "start", _succeed)
        subscribe = AsyncMock(return_value=1)
        monkeypatch.setattr(reporting, "subscribe_all", subscribe)

        resp = await async_client.post("/api/v1/zigbee/restart")

        assert resp.status_code == 200
        subscribe.assert_awaited_once()
        assert [p.plug_type for p in subscribe.await_args.args[1]] == ["zigbee"]
