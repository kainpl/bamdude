"""The manager must send MQTT plugs to the MQTT service.

It used to fall through to Tasmota, which issues HTTP to ``plug.ip_address`` —
a field an MQTT plug has no reason to have — so every switch silently no-opped.
"""

import inspect
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_mqtt_plug_gets_the_mqtt_service():
    from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service
    from backend.app.services.smart_plug_manager import SmartPlugManager

    manager = SmartPlugManager()
    plug = SimpleNamespace(id=1, plug_type="mqtt")
    assert await manager.get_service_for_plug(plug) is mqtt_smart_plug_service


@pytest.mark.asyncio
async def test_tasmota_is_still_the_default():
    from backend.app.services.smart_plug_manager import SmartPlugManager
    from backend.app.services.tasmota import tasmota_service

    manager = SmartPlugManager()
    plug = SimpleNamespace(id=1, plug_type="tasmota")
    assert await manager.get_service_for_plug(plug) is tasmota_service


def test_snapshot_loop_no_longer_special_cases_mqtt():
    """The blanket skip is gone; the `total is None` guard below it suffices.

    That guard already does the right thing for a plug with no lifetime source,
    so the type-level skip was both wrong (Zigbee2MQTT reports a cumulative
    figure) and redundant.
    """
    from backend.app.services import smart_plug_manager

    source = inspect.getsource(smart_plug_manager.SmartPlugManager._capture_energy_snapshots)
    assert 'plug_type == "mqtt"' not in source
    assert 'energy.get("total")' in source
