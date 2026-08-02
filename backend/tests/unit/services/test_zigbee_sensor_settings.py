"""Operator-set reporting parameters, defaulted from the registry.

Stored in the existing key/value settings table as one JSON blob, which is the
pattern drying_presets and ams_humidity_thresholds already use — and which is
why this cycle needs no migration.

Every loader is total: these are read from a pairing callback and a background
loop, where an exception is a feature that silently stops working.
"""

import json

import pytest

from backend.app.services.zigbee.sensor_settings import (
    load_poll_seconds,
    load_reporting_parameters,
    load_stale_multiplier,
)


class _Db:
    """Stands in for the session; only get_setting reads it."""

    def __init__(self, values):
        self.values = values


@pytest.fixture(autouse=True)
def patched_settings(monkeypatch):
    async def fake_get_setting(db, key):
        return db.values.get(key)

    monkeypatch.setattr("backend.app.services.zigbee.sensor_settings.get_setting", fake_get_setting)


@pytest.mark.asyncio
async def test_defaults_come_from_the_registry_when_nothing_is_stored():
    parameters = await load_reporting_parameters(_Db({}))

    assert parameters["temperature"]["min_interval"] == 30
    assert parameters["temperature"]["max_interval"] == 900
    assert parameters["temperature"]["reportable_change"] == 0.1
    assert parameters["battery"]["max_interval"] == 10800


@pytest.mark.asyncio
async def test_a_stored_value_overrides_one_field_and_leaves_the_rest():
    db = _Db({"zigbee_sensor_reporting": json.dumps({"temperature": {"max_interval": 600}})})

    parameters = await load_reporting_parameters(db)

    assert parameters["temperature"]["max_interval"] == 600
    assert parameters["temperature"]["min_interval"] == 30, "unset fields keep the registry default"


@pytest.mark.asyncio
async def test_unparseable_json_falls_back_to_defaults():
    """A corrupt setting must not take the sensors down with it."""
    parameters = await load_reporting_parameters(_Db({"zigbee_sensor_reporting": "{not json"}))

    assert parameters["temperature"]["min_interval"] == 30


@pytest.mark.asyncio
async def test_an_unknown_measurement_key_is_ignored():
    db = _Db({"zigbee_sensor_reporting": json.dumps({"radiation": {"min_interval": 5}})})

    parameters = await load_reporting_parameters(db)

    assert "radiation" not in parameters


@pytest.mark.asyncio
async def test_a_non_numeric_field_keeps_the_default_instead_of_breaking():
    db = _Db({"zigbee_sensor_reporting": json.dumps({"temperature": {"min_interval": "often"}})})

    parameters = await load_reporting_parameters(db)

    assert parameters["temperature"]["min_interval"] == 30


@pytest.mark.asyncio
async def test_the_stale_multiplier_and_poll_interval_have_defaults():
    assert await load_stale_multiplier(_Db({})) == 2.0
    assert await load_poll_seconds(_Db({})) == 30


@pytest.mark.asyncio
async def test_nonsense_scalars_fall_back_rather_than_break():
    assert await load_stale_multiplier(_Db({"zigbee_sensor_stale_multiplier": "soon"})) == 2.0
    assert await load_poll_seconds(_Db({"zigbee_sensor_poll_seconds": "0"})) == 30


@pytest.mark.asyncio
async def test_stored_scalars_are_honoured():
    assert await load_stale_multiplier(_Db({"zigbee_sensor_stale_multiplier": "3.5"})) == 3.5
    assert await load_poll_seconds(_Db({"zigbee_sensor_poll_seconds": "120"})) == 120
