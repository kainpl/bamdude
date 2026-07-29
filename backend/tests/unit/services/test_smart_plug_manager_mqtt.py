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


def test_control_endpoint_no_longer_rejects_mqtt():
    """The endpoint used to 400 every MQTT control request as monitor-only.

    Without this the automation paths (auto-on, auto-off, schedules, Obico's
    pause_and_off) would work while the buttons in the UI kept failing — a
    half-working feature that reads as a bug.
    """
    from backend.app.api.routes import smart_plugs

    assert "MQTT plugs are monitor-only" not in inspect.getsource(smart_plugs)


def test_energy_reads_go_through_the_driver():
    """Three copies of the MQTT energy mapping, and two had already drifted.

    ``_get_plug_energy`` and the archives aggregate each had their own MQTT
    branch that filed the cached reading as "today" and never set "total" —
    so a correct driver would have been bypassed by exactly the paths that
    feed per-print energy.
    """
    from backend.app import main
    from backend.app.api.routes import archives

    assert "MQTT plugs report" not in inspect.getsource(main._get_plug_energy)
    assert "MQTT plugs only expose today" not in inspect.getsource(archives)
