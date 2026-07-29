"""The boundary that keeps this project about plugs.

Without a gate in code, "sensors are out of scope" erodes one sympathetic
request at a time: a temperature sensor pairs, someone asks to show its reading,
and the scope has quietly become Zigbee2MQTT.
"""

from types import SimpleNamespace

from backend.app.services.zigbee.devices import (
    ELECTRICAL_MEASUREMENT,
    METERING,
    ON_OFF,
    describe_device,
)


def _device(*endpoint_clusters, ieee="34:8d:13:ff:fe:11:e4:6f", model="S60ZBTPF"):
    # Endpoint 0 is the ZDO and never carries application clusters.
    endpoints = {0: SimpleNamespace(in_clusters={})}
    for idx, clusters in enumerate(endpoint_clusters, start=1):
        endpoints[idx] = SimpleNamespace(in_clusters={c: object() for c in clusters})
    return SimpleNamespace(ieee=ieee, nwk=0x1234, manufacturer="SONOFF", model=model, endpoints=endpoints)


def test_plug_with_metering_is_accepted_with_both_capabilities():
    info = describe_device(_device([ON_OFF, METERING, ELECTRICAL_MEASUREMENT]))

    assert info.is_plug is True
    assert info.has_metering is True
    assert info.has_electrical_measurement is True
    assert info.reject_reason is None


def test_plug_without_metering_is_still_accepted():
    """It can be switched. Energy simply stays unavailable — and phase 3 has to
    know that up front rather than discovering a silent zero."""
    info = describe_device(_device([ON_OFF]))

    assert info.is_plug is True
    assert info.has_metering is False
    assert info.has_electrical_measurement is False
    assert info.reject_reason is None


def test_device_without_on_off_is_rejected_with_a_reason():
    info = describe_device(_device([METERING], model="TH01"))

    assert info.is_plug is False
    assert "On/Off" in info.reject_reason


def test_on_off_on_a_later_endpoint_still_counts():
    """The cluster does not have to live on endpoint 1, and plugs do move it."""
    info = describe_device(_device([METERING], [ON_OFF]))

    assert info.is_plug is True


def test_device_with_no_endpoints_at_all_is_rejected():
    """A partial interview must not read as a plug."""
    info = describe_device(
        SimpleNamespace(ieee="00:00:00:00:00:00:00:01", nwk=0x1, manufacturer=None, model=None, endpoints={})
    )

    assert info.is_plug is False
    assert info.reject_reason


def test_endpoints_none_is_survivable():
    """zigpy can hand us a device before endpoints are populated."""
    info = describe_device(
        SimpleNamespace(ieee="00:00:00:00:00:00:00:02", nwk=0x2, manufacturer=None, model=None, endpoints=None)
    )

    assert info.is_plug is False


def test_identity_fields_survive_missing_manufacturer_and_model():
    """zigpy leaves these None on a partial interview; the API must not 500."""
    info = describe_device(_device([ON_OFF], model=None))

    assert info.model is None
    assert info.manufacturer == "SONOFF"
    assert info.ieee == "34:8d:13:ff:fe:11:e4:6f"
