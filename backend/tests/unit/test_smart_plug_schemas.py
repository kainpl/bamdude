"""Schema round-trip for the MQTT control + lifetime-energy fields."""

import pytest
from pydantic import ValidationError

from backend.app.schemas.smart_plug import SmartPlugCreate, SmartPlugUpdate


def test_create_accepts_control_and_total_fields():
    plug = SmartPlugCreate(
        name="Printer plug",
        plug_type="mqtt",
        mqtt_power_topic="zigbee2mqtt/plug",
        mqtt_power_path="power",
        mqtt_energy_total_topic="zigbee2mqtt/plug",
        mqtt_energy_total_path="energy",
        mqtt_energy_total_multiplier=0.001,
        mqtt_command_topic="zigbee2mqtt/plug/set",
        mqtt_command_on='{"state": "ON"}',
        mqtt_command_off='{"state": "OFF"}',
    )
    assert plug.mqtt_command_topic == "zigbee2mqtt/plug/set"
    assert plug.mqtt_energy_total_multiplier == 0.001


def test_monitor_only_plug_still_validates():
    """No command topic is valid configuration, not an error.

    A clamp meter reporting a printer's draw is not a switch, and every plug
    that existed before 0.5.x is this shape.
    """
    plug = SmartPlugCreate(
        name="Clamp meter",
        plug_type="mqtt",
        mqtt_power_topic="shellies/clamp",
        mqtt_power_path="power",
    )
    assert plug.mqtt_command_topic is None


def test_mqtt_plug_still_needs_a_monitoring_topic():
    """A command topic alone is not a data source."""
    with pytest.raises(ValidationError):
        SmartPlugCreate(
            name="Broken",
            plug_type="mqtt",
            mqtt_command_topic="zigbee2mqtt/plug/set",
        )


def test_update_leaves_unset_fields_none():
    update = SmartPlugUpdate(mqtt_command_on='{"state": "ON"}')
    assert update.mqtt_command_on == '{"state": "ON"}'
    assert update.mqtt_command_topic is None


def test_total_multiplier_rejects_zero():
    with pytest.raises(ValidationError):
        SmartPlugCreate(
            name="Bad",
            plug_type="mqtt",
            mqtt_power_topic="t",
            mqtt_energy_total_multiplier=0,
        )
