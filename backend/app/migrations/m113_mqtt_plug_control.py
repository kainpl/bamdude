"""Give MQTT smart plugs a control channel and a lifetime-energy source.

The ``mqtt`` plug type has been monitor-only since it was added. The model
carries topics for power, energy and state but nothing to publish to, and
``smart_plug_manager.get_service_for_plug`` had no ``mqtt`` branch — it fell
through to the Tasmota service, which issues HTTP to ``plug.ip_address``, a
field an MQTT-attached plug has no reason to have. Every turn_on/turn_off on
such a plug silently did nothing: auto-on at print start, auto-off afterwards,
schedules, the manual buttons, and Obico's pause_and_off.

``mqtt_command_topic`` plus the two payload columns give it a command channel.
The payloads are free-form text because they belong to the device: Zigbee2MQTT
wants '{"state": "ON"}' on <name>/set, Tasmota wants a bare "ON" on
cmnd/<name>/POWER. Modelling that as an enum would be wrong for the third
device we meet.

``mqtt_energy_total_*`` mirrors what m110 did for REST (upstream #2539).
``_capture_energy_snapshots`` skipped MQTT plugs outright, reasoning they "only
publish a today counter that resets at midnight" — true of Tasmota's
``ENERGY.Today``, false of Zigbee2MQTT's ``energy``, which is a lifetime total.
Which one a plug reports depends on where the operator pointed
``mqtt_energy_path``, so it cannot be decided in code. A separate path for the
lifetime figure is the answer REST already uses; presence of the path is the
signal, which is why there is no "is it cumulative" boolean here.

Additive and nullable, so existing rows are untouched: a plug without a command
topic stays monitor-only, and one without a total path still feeds no snapshots
— exactly today's behaviour in both cases.
"""

from backend.app.migrations.helpers import add_column

version = 113
name = "mqtt_plug_control"


async def upgrade(conn):
    await add_column(conn, "smart_plugs", "mqtt_energy_total_topic VARCHAR(200)")
    await add_column(conn, "smart_plugs", "mqtt_energy_total_path VARCHAR(100)")
    await add_column(conn, "smart_plugs", "mqtt_energy_total_multiplier FLOAT DEFAULT 1.0 NOT NULL")
    await add_column(conn, "smart_plugs", "mqtt_command_topic VARCHAR(200)")
    await add_column(conn, "smart_plugs", "mqtt_command_on TEXT")
    await add_column(conn, "smart_plugs", "mqtt_command_off TEXT")
