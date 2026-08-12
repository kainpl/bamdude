"""What the device STORED, not what it answered.

A device may return SUCCESS and silently keep different intervals — its own
firmware limits, quietly applied. Today that reads as configured, and the
operator looks at a number the device does not hold. ZHA does not detect this
at all.

zigpy has no convenience method for reading a reporting configuration back:
``Cluster`` exposes ``configure_reporting`` and not its counterpart, so this
goes out as a ZCL general command.
"""

import pytest
from zigpy.zcl import foundation


class _Rsp:
    def __init__(self, configs):
        self.attribute_configs = configs


def _entry(status, *, min_interval=None, max_interval=None, change=None, attrid=0x0000):
    config = foundation.AttributeReportingConfig()
    config.direction = foundation.ReportingDirection.SendReports
    config.attrid = attrid
    if status == foundation.Status.SUCCESS:
        config.min_interval = min_interval
        config.max_interval = max_interval
        config.reportable_change = change
    return foundation.AttributeReportingConfigWithStatus(status=status, config=config)


class _Cluster:
    """A stub that answers with values DIFFERENT from any request.

    This is the point of the whole file: a stub that echoes what it was asked
    keeps the mismatch test green for ever while testing nothing.
    """

    def __init__(self, rsp=None, raises=None, attributes_by_name=None):
        self._rsp = rsp
        self._raises = raises
        self.calls = []
        if attributes_by_name is not None:
            self.attributes_by_name = attributes_by_name

    async def general_command(self, command_id, *args, **kwargs):
        self.calls.append((command_id, args))
        if self._raises:
            raise self._raises
        return self._rsp


@pytest.mark.asyncio
async def test_a_successful_read_returns_what_the_device_holds():
    from backend.app.services.zigbee.verification import read_reporting_back

    cluster = _Cluster(_Rsp([_entry(foundation.Status.SUCCESS, min_interval=60, max_interval=1200, change=5)]))

    assert await read_reporting_back(cluster, 0x0000) == {
        "min_interval": 60,
        "max_interval": 1200,
        "reportable_change": 5,
    }


@pytest.mark.asyncio
async def test_a_non_success_status_reads_as_nothing_known():
    from backend.app.services.zigbee.verification import read_reporting_back

    cluster = _Cluster(_Rsp([_entry(foundation.Status.UNSUPPORTED_ATTRIBUTE)]))

    assert await read_reporting_back(cluster, 0x0000) is None


@pytest.mark.asyncio
async def test_a_device_that_does_not_answer_reads_as_nothing_known():
    """A sleeper that dozed off between the write and the read. Not a fault, and
    specifically not a mismatch."""
    from backend.app.services.zigbee.verification import read_reporting_back

    cluster = _Cluster(raises=TimeoutError())

    assert await read_reporting_back(cluster, 0x0000) is None


@pytest.mark.asyncio
async def test_an_answer_about_a_different_attribute_is_not_taken_as_ours():
    """One request, one record — but a device is free to answer with more, and
    filing somebody else's configuration under our attribute would report a
    mismatch that does not exist."""
    from backend.app.services.zigbee.verification import read_reporting_back

    cluster = _Cluster(
        _Rsp([_entry(foundation.Status.SUCCESS, min_interval=1, max_interval=2, change=3, attrid=0x0055)])
    )

    assert await read_reporting_back(cluster, 0x0000) is None


@pytest.mark.asyncio
async def test_the_read_asks_for_the_send_reports_direction():
    """Direction 0x01 asks about reports we RECEIVE, which is a different
    question and answers with a timeout value instead of intervals."""
    from backend.app.services.zigbee.verification import read_reporting_back

    cluster = _Cluster(
        _Rsp([_entry(foundation.Status.SUCCESS, min_interval=1, max_interval=2, change=3, attrid=0x0042)])
    )
    await read_reporting_back(cluster, 0x0042)

    command_id, args = cluster.calls[0]
    assert command_id == foundation.GeneralCommand.Read_Reporting_Configuration
    record = args[0][0]
    assert record.direction == foundation.ReportingDirection.SendReports
    assert record.attrid == 0x0042


class TestAttributeNames:
    """Sensor targets carry names, plug targets carry ids — but the ZCL record
    on the wire only takes an id. Resolving belongs to the cluster, which is the
    only thing that knows the mapping for its own model."""

    @pytest.mark.asyncio
    async def test_a_name_is_resolved_through_the_cluster(self):
        from zigpy.zcl.clusters.measurement import TemperatureMeasurement

        from backend.app.services.zigbee.verification import read_reporting_back

        cluster = _Cluster(
            _Rsp([_entry(foundation.Status.SUCCESS, min_interval=30, max_interval=900, change=10)]),
            attributes_by_name=TemperatureMeasurement.attributes_by_name,
        )

        assert await read_reporting_back(cluster, "measured_value") is not None
        assert cluster.calls[0][1][0][0].attrid == 0x0000

    @pytest.mark.asyncio
    async def test_a_name_the_cluster_does_not_know_asks_nothing(self):
        """Better to learn nothing than to send a request naming attribute
        zero, whose answer would then be compared against another attribute's
        configuration."""
        from zigpy.zcl.clusters.measurement import TemperatureMeasurement

        from backend.app.services.zigbee.verification import read_reporting_back

        cluster = _Cluster(
            _Rsp([_entry(foundation.Status.SUCCESS)]),
            attributes_by_name=TemperatureMeasurement.attributes_by_name,
        )

        assert await read_reporting_back(cluster, "radiation_level") is None
        assert cluster.calls == []

    @pytest.mark.asyncio
    async def test_a_cluster_without_the_mapping_does_not_raise(self):
        from backend.app.services.zigbee.verification import read_reporting_back

        cluster = _Cluster(_Rsp([_entry(foundation.Status.SUCCESS)]))

        assert await read_reporting_back(cluster, "measured_value") is None


class TestCompare:
    def test_identical_is_verified(self):
        from backend.app.services.zigbee.verification import compare

        desired = {"min_interval": 30, "max_interval": 900, "reportable_change": 10}
        assert compare(desired, dict(desired)) == "verified"

    def test_a_different_interval_is_a_mismatch(self):
        """The case this whole task exists for: accepted with SUCCESS, stored
        differently."""
        from backend.app.services.zigbee.verification import compare

        desired = {"min_interval": 30, "max_interval": 900, "reportable_change": 10}
        assert compare(desired, {**desired, "min_interval": 60}) == "mismatch"

    def test_a_different_change_is_a_mismatch_too(self):
        from backend.app.services.zigbee.verification import compare

        desired = {"min_interval": 30, "max_interval": 900, "reportable_change": 10}
        assert compare(desired, {**desired, "reportable_change": 1}) == "mismatch"

    def test_nothing_read_back_is_not_a_mismatch(self):
        from backend.app.services.zigbee.verification import compare

        assert compare({"min_interval": 30, "max_interval": 900, "reportable_change": 10}, None) == "not-checked"

    def test_an_absent_field_is_not_compared(self):
        """Some devices omit reportable_change for discrete attributes, and
        reading that as a mismatch would cry wolf on every relay."""
        from backend.app.services.zigbee.verification import compare

        desired = {"min_interval": 0, "max_interval": 900, "reportable_change": 1}
        assert compare(desired, {"min_interval": 0, "max_interval": 900}) == "verified"

    def test_a_float_change_compares_by_value_not_by_type(self):
        """A device answers with its own numeric type; 10 and 10.0 are the same
        configuration."""
        from backend.app.services.zigbee.verification import compare

        assert compare({"reportable_change": 10}, {"reportable_change": 10.0}) == "verified"
