"""Control + energy reporting for MQTT smart plugs.

Before 0.5.x this type had no driver methods at all: ``get_service_for_plug``
fell through to the Tasmota service, which sends HTTP to ``plug.ip_address`` —
a field an MQTT-attached plug has no reason to have — so every switch operation
silently did nothing.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.services.mqtt_smart_plug import MQTTSmartPlugService, SmartPlugMQTTData


def _plug(**overrides):
    base = {
        "id": 1,
        "mqtt_command_topic": "zigbee2mqtt/plug/set",
        "mqtt_command_on": '{"state": "ON"}',
        "mqtt_command_off": '{"state": "OFF"}',
        "mqtt_state_topic": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _connected(service):
    service.client = MagicMock()
    service.client.publish.return_value = MagicMock(rc=0)
    service.connected = True
    return service


class TestPublish:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_connected(self):
        service = MQTTSmartPlugService()
        service.client = None
        service.connected = False
        assert await service._publish("t", "p") is False

    @pytest.mark.asyncio
    async def test_publishes_at_qos1_without_retain(self):
        """A retained command would re-fire on every broker reconnect."""
        service = _connected(MQTTSmartPlugService())
        assert await service._publish("t", "p") is True
        _, kwargs = service.client.publish.call_args
        assert kwargs.get("qos") == 1
        assert kwargs.get("retain") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_broker_rejection(self):
        service = _connected(MQTTSmartPlugService())
        service.client.publish.return_value = MagicMock(rc=4)
        assert await service._publish("t", "p") is False


class TestTurnOnOff:
    @pytest.mark.asyncio
    async def test_publishes_the_exact_configured_payload(self):
        service = _connected(MQTTSmartPlugService())
        assert await service.turn_on(_plug()) is True
        args, _ = service.client.publish.call_args
        assert args[0] == "zigbee2mqtt/plug/set"
        assert args[1] == '{"state": "ON"}'

    @pytest.mark.asyncio
    async def test_turn_off_uses_the_off_payload(self):
        service = _connected(MQTTSmartPlugService())
        await service.turn_off(_plug())
        args, _ = service.client.publish.call_args
        assert args[1] == '{"state": "OFF"}'

    @pytest.mark.asyncio
    async def test_monitor_only_plug_publishes_nothing(self):
        """No command topic is valid configuration, not an error."""
        service = _connected(MQTTSmartPlugService())
        assert await service.turn_on(_plug(mqtt_command_topic=None)) is False
        service.client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirms_from_the_state_cache(self):
        service = _connected(MQTTSmartPlugService())
        service.plug_data[1] = SmartPlugMQTTData(plug_id=1, state="ON")
        assert await service.turn_on(_plug(mqtt_state_topic="zigbee2mqtt/plug")) is True

    @pytest.mark.asyncio
    async def test_returns_true_on_confirm_timeout(self, monkeypatch):
        """QoS 1 delivered it; a False would mark a healthy plug failed."""
        service = _connected(MQTTSmartPlugService())
        monkeypatch.setattr(service, "CONTROL_CONFIRM_TIMEOUT_SECONDS", 0.05)
        service.plug_data[1] = SmartPlugMQTTData(plug_id=1, state="OFF")
        assert await service.turn_on(_plug(mqtt_state_topic="zigbee2mqtt/plug")) is True


class TestToggle:
    @pytest.mark.asyncio
    async def test_toggles_off_when_currently_on(self):
        service = _connected(MQTTSmartPlugService())
        service.plug_data[1] = SmartPlugMQTTData(plug_id=1, state="ON")
        assert await service.toggle(_plug()) is True
        args, _ = service.client.publish.call_args
        assert args[1] == '{"state": "OFF"}'

    @pytest.mark.asyncio
    async def test_refuses_when_state_unknown(self):
        """Switching a printer's mains on a guess is worse than declining."""
        service = _connected(MQTTSmartPlugService())
        assert await service.toggle(_plug()) is False
        service.client.publish.assert_not_called()


class TestGetEnergy:
    @pytest.mark.asyncio
    async def test_returns_none_with_empty_cache(self):
        service = MQTTSmartPlugService()
        assert await service.get_energy(_plug()) is None

    @pytest.mark.asyncio
    async def test_maps_energy_to_today_and_total_to_total(self):
        service = MQTTSmartPlugService()
        service.plug_data[1] = SmartPlugMQTTData(plug_id=1, power=42.0, energy=1.5, energy_total=99.5)
        result = await service.get_energy(_plug())
        assert result["power"] == 42.0
        assert result["today"] == 1.5
        assert result["total"] == 99.5

    @pytest.mark.asyncio
    async def test_omits_total_when_no_lifetime_value(self):
        """Snapshots skip a plug whose total is None — that must stay true."""
        service = MQTTSmartPlugService()
        service.plug_data[1] = SmartPlugMQTTData(plug_id=1, energy=1.5)
        result = await service.get_energy(_plug())
        assert "total" not in result


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_unreachable_when_no_data(self):
        service = MQTTSmartPlugService()
        assert await service.get_status(_plug()) == {"state": None, "reachable": False, "device_name": None}

    @pytest.mark.asyncio
    async def test_reports_cached_state(self):
        service = MQTTSmartPlugService()
        service.plug_data[1] = SmartPlugMQTTData(plug_id=1, state="ON")
        status = await service.get_status(_plug())
        assert status["state"] == "ON"
        assert status["reachable"] is True


class TestReachabilityDefault:
    """Regression: a subscribed-but-silent plug used to crash is_reachable.

    ``SmartPlugMQTTData.last_seen`` defaulted to a naive ``datetime.utcnow()``
    while ``is_reachable`` subtracts it from an aware ``datetime.now(utc)``.
    Between adding a plug and its device's first publish, the status endpoint —
    which calls ``is_reachable`` unconditionally — raised TypeError.
    """

    def test_default_last_seen_is_timezone_aware(self):
        assert SmartPlugMQTTData(plug_id=1).last_seen.tzinfo is not None

    def test_is_reachable_on_a_freshly_subscribed_plug(self):
        service = MQTTSmartPlugService()
        service.subscribe(plug_id=1, power_topic="t", power_path="power")
        assert service.is_reachable(1) is True
