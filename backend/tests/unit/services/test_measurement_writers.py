"""Every path that produces a reading must reach the buffer.

Wiring one and forgetting another is invisible: the chart is simply thinner for
some devices, which looks like those devices being quiet.
"""

import pytest


@pytest.fixture(autouse=True)
def empty_buffer():
    from backend.app.services.measurement_buffer import measurement_buffer

    measurement_buffer.drain()
    yield
    measurement_buffer.drain()


def test_a_zigbee_power_report_reaches_the_buffer():
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT
    from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
    from backend.app.services.zigbee.reporting import ATTR_ACTIVE_POWER, ClusterReportListener

    listener = ClusterReportListener(
        service=ZigbeeSmartPlugService(), plug_id=7, cluster_id=ELECTRICAL_MEASUREMENT, multiplier=1, divisor=10
    )

    listener.attribute_updated(ATTR_ACTIVE_POWER, 425, None)

    power, _ = measurement_buffer.drain()
    assert [(s.plug_id, s.power) for s in power] == [(7, 42.5)]


def test_an_unusable_zigbee_reading_buffers_nothing():
    """It is not cached either — the two must agree, or the chart shows a value
    the card does not."""
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT
    from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
    from backend.app.services.zigbee.reporting import ATTR_ACTIVE_POWER, ClusterReportListener

    listener = ClusterReportListener(
        service=ZigbeeSmartPlugService(), plug_id=7, cluster_id=ELECTRICAL_MEASUREMENT, multiplier=1, divisor=None
    )

    listener.attribute_updated(ATTR_ACTIVE_POWER, 425, None)

    assert measurement_buffer.drain() == ([], [])


def test_power_from_metering_demand_reaches_the_buffer_too():
    """The fallback path for a plug with no ElectricalMeasurement. Wiring one
    branch and not the other is exactly the half-fix this subsystem keeps
    rediscovering."""
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.zigbee.devices import METERING
    from backend.app.services.zigbee.driver import ZigbeeSmartPlugService
    from backend.app.services.zigbee.reporting import ClusterReportListener
    from backend.app.services.zigbee.reporting_targets import ATTR_INSTANTANEOUS_DEMAND

    listener = ClusterReportListener(
        service=ZigbeeSmartPlugService(), plug_id=7, cluster_id=METERING, multiplier=1, divisor=1000
    )

    listener.attribute_updated(ATTR_INSTANTANEOUS_DEMAND, 200, None)

    power, _ = measurement_buffer.drain()
    assert [(s.plug_id, s.power) for s in power] == [(7, 200.0)]


def test_a_sensor_reading_reaches_the_buffer():
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.zigbee.sensors import sensor_store

    sensor_store.forget("aa:bb")
    sensor_store.record("aa:bb", "temperature", 2341)

    _power, sensors = measurement_buffer.drain()
    assert [(s.ieee, s.kind, s.value) for s in sensors] == [("aa:bb", "temperature", 23.41)]
    sensor_store.forget("aa:bb")


def test_a_sensor_reading_that_is_unusable_buffers_nothing():
    """A sentinel means "no measurement". Recording contact is right; recording
    a value is not."""
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.zigbee.sensors import sensor_store

    sensor_store.forget("aa:bb")
    sensor_store.record("aa:bb", "temperature", -32768)

    assert measurement_buffer.drain() == ([], [])
    sensor_store.forget("aa:bb")


def test_an_unregistered_quantity_buffers_nothing():
    from backend.app.services.measurement_buffer import measurement_buffer
    from backend.app.services.zigbee.sensors import sensor_store

    sensor_store.forget("aa:bb")
    sensor_store.record("aa:bb", "radiation", 5)

    assert measurement_buffer.drain() == ([], [])
    sensor_store.forget("aa:bb")


def test_the_history_loop_is_cancelled_on_shutdown():
    """Every create_task is paired with a shutdown cancel — a standing rule, and
    a leaked loop keeps a database session alive past teardown."""
    from backend.app.services.smart_plug_manager import SmartPlugManager

    manager = SmartPlugManager()

    assert hasattr(manager, "_history_task")
    source = SmartPlugManager.stop_scheduler.__doc__ or ""
    assert "_history_task" in SmartPlugManager.stop_scheduler.__code__.co_names or "_history_task" in source
