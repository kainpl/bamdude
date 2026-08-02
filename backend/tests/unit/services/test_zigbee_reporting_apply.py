"""Two orthogonal facts, never folded into one word.

``state`` is what the device answered to ``configure_reporting``. ``verification``
is what reading it back said. Folding them forces a wrong answer on the ordinary
case — configure succeeded, the read-back did not arrive — which for a battery
device is most of the time.
"""

import pytest
from zigpy.zcl import foundation

from backend.app.services.zigbee.reporting_targets import ReportingTarget


def _target(key="temperature", cluster=0x0402, attribute=0x0000, editable=None):
    return ReportingTarget(
        key=key,
        cluster=cluster,
        attribute=attribute,
        min_interval=30,
        max_interval=900,
        reportable_change=0.1,
        editable=editable or ("min_interval", "max_interval", "reportable_change"),
        to_raw=lambda change, scaling=None: 10,
    )


class _Cluster:
    def __init__(self, *, configure=None, read_back=None, bind=None):
        self._configure = configure if configure is not None else {}
        self._read_back = read_back
        self._bind = bind
        self.configured = []
        self.bound = 0

    async def bind(self):
        self.bound += 1
        if isinstance(self._bind, Exception):
            raise self._bind
        return [foundation.Status.SUCCESS]

    async def configure_reporting(self, attribute, minimum, maximum, change):
        self.configured.append((attribute, minimum, maximum, change))
        if isinstance(self._configure, Exception):
            raise self._configure
        return self._configure

    async def general_command(self, command_id, *args, **kwargs):
        if self._read_back is None:
            raise TimeoutError()
        return self._read_back


def _rsp(min_interval, max_interval, change, attrid=0x0000):
    config = foundation.AttributeReportingConfig()
    config.direction = foundation.ReportingDirection.SendReports
    config.attrid = attrid
    config.min_interval = min_interval
    config.max_interval = max_interval
    config.reportable_change = change
    entry = foundation.AttributeReportingConfigWithStatus(status=foundation.Status.SUCCESS, config=config)
    return type("Rsp", (), {"attribute_configs": [entry]})()


DESIRED = {"temperature": {"min_interval": 30, "max_interval": 900, "reportable_change": 0.1}}


@pytest.mark.asyncio
async def test_accepted_and_confirmed_is_ok_and_verified():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(read_back=_rsp(30, 900, 10))
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"] == {"state": "ok", "verification": "verified"}


@pytest.mark.asyncio
async def test_accepted_but_unconfirmed_is_ok_and_not_checked():
    """The ordinary case for a sleeper, and the one a single vocabulary cannot
    express without lying in one direction or the other."""
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(read_back=None)
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"] == {"state": "ok", "verification": "not-checked"}


@pytest.mark.asyncio
async def test_silently_clamped_is_ok_and_mismatch():
    """The device answered SUCCESS and stored something else. The stub returns
    values that were never requested — one echoing the request would keep this
    test green while testing nothing."""
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(read_back=_rsp(60, 300, 10))
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"] == {"state": "ok", "verification": "mismatch"}


@pytest.mark.asyncio
async def test_an_explicit_refusal_is_refused():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(configure={"measured_value": foundation.Status.UNSUPPORTED_ATTRIBUTE})
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"]["state"] == "refused"


@pytest.mark.asyncio
async def test_a_refusal_in_the_record_shape_is_also_refused():
    """zigpy answers with records, not a dict, on the plug path. Both shapes
    have to be understood or one class silently reads every refusal as success."""
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    record = foundation.ConfigureReportingResponseRecord(
        status=foundation.Status.UNSUPPORTED_ATTRIBUTE,
        direction=foundation.ReportingDirection.SendReports,
        attrid=0x0000,
    )
    cluster = _Cluster(configure=[[record]])
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"]["state"] == "refused"


@pytest.mark.asyncio
async def test_a_success_record_is_accepted():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    record = foundation.ConfigureReportingResponseRecord(
        status=foundation.Status.SUCCESS,
        direction=foundation.ReportingDirection.SendReports,
        attrid=0x0000,
    )
    cluster = _Cluster(configure=[[record]], read_back=_rsp(30, 900, 10))
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"]["state"] == "ok"


@pytest.mark.asyncio
async def test_no_answer_is_unanswered_not_refused():
    """A sleeper that never woke declined nothing. Reported as refused, it sends
    an operator hunting a fault in hardware that works — and it would not be
    retried, which is the worse half."""
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(configure=TimeoutError())
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"] == {"state": "unanswered", "verification": "not-checked"}


@pytest.mark.asyncio
async def test_a_bind_that_fails_is_unanswered_too():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(bind=TimeoutError())
    result = await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert result["temperature"]["state"] == "unanswered"


@pytest.mark.asyncio
async def test_a_missing_cluster_is_unanswered_and_costs_the_others_nothing():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    targets = (_target("temperature", 0x0402), _target("humidity", 0x0405))
    cluster = _Cluster(read_back=_rsp(30, 900, 10))

    def cluster_for(cluster_id):
        return cluster if cluster_id == 0x0405 else None

    result = await apply_reporting(
        cluster_for,
        "aa:bb",
        targets,
        {**DESIRED, "humidity": DESIRED["temperature"]},
    )

    assert result["temperature"]["state"] == "unanswered"
    assert result["humidity"]["state"] == "ok"


@pytest.mark.asyncio
async def test_one_target_failing_does_not_cost_the_others():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    good = _Cluster(read_back=_rsp(30, 900, 10))
    bad = _Cluster(configure=TimeoutError())

    def cluster_for(cluster_id):
        return bad if cluster_id == 0x0402 else good

    result = await apply_reporting(
        cluster_for,
        "aa:bb",
        (_target("temperature", 0x0402), _target("humidity", 0x0405)),
        {**DESIRED, "humidity": DESIRED["temperature"]},
    )

    assert result["temperature"]["state"] == "unanswered"
    assert result["humidity"]["state"] == "ok"


@pytest.mark.asyncio
async def test_the_desired_values_reach_the_device_converted():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(read_back=_rsp(30, 900, 10))
    await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), DESIRED)

    assert cluster.configured == [(0x0000, 30, 900, 10)]


@pytest.mark.asyncio
async def test_a_target_with_nothing_desired_falls_back_to_its_own_defaults():
    """Resolution has three layers above this, but the loop itself must still
    be safe to call with a key the caller forgot."""
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    cluster = _Cluster(read_back=_rsp(30, 900, 10))
    await apply_reporting(lambda _c: cluster, "aa:bb", (_target(),), {})

    assert cluster.configured == [(0x0000, 30, 900, 10)]


@pytest.mark.asyncio
async def test_the_scaling_is_handed_to_the_target_conversion():
    """A plug's raw change depends on the multiplier and divisor its own
    firmware reports, and the apply loop is where both are in hand."""
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    seen = {}

    def to_raw(change, scaling=None):
        seen["scaling"] = scaling
        return 7

    target = ReportingTarget(
        key="power",
        cluster=0x0B04,
        attribute=0x050B,
        min_interval=5,
        max_interval=900,
        reportable_change=1,
        editable=("min_interval", "max_interval", "reportable_change"),
        to_raw=to_raw,
    )
    cluster = _Cluster(read_back=_rsp(5, 900, 7, attrid=0x050B))
    await apply_reporting(lambda _c: cluster, "aa:bb", (target,), {}, scaling=(1, 1000))

    assert seen["scaling"] == (1, 1000)
    assert cluster.configured == [(0x050B, 5, 900, 7)]


@pytest.mark.asyncio
async def test_nothing_to_apply_is_not_an_error():
    from backend.app.services.zigbee.reporting_apply import apply_reporting

    assert await apply_reporting(lambda _c: None, "aa:bb", (), {}) == {}


class TestOnlyACompleteApplyCounts:
    """What may be recorded as "this device is running what we asked".

    Anything less has to be retried at the next contact. Recording regardless
    would settle the configuration for ever on the strength of one moment — and
    for a mismatch it is worse than that: the device is running numbers nobody
    chose, and re-issuing is the only thing that could ever correct it.
    """

    def test_accepted_and_verified_counts(self):
        from backend.app.services.zigbee.reporting_apply import fully_applied

        assert fully_applied({"temperature": {"state": "ok", "verification": "verified"}}) is True

    def test_accepted_but_unverified_counts(self):
        """Otherwise a battery sensor, whose read-back rarely lands in the same
        wake window, would be re-configured on every single report for ever."""
        from backend.app.services.zigbee.reporting_apply import fully_applied

        assert fully_applied({"temperature": {"state": "ok", "verification": "not-checked"}}) is True

    def test_a_mismatch_does_not_count(self):
        from backend.app.services.zigbee.reporting_apply import fully_applied

        assert fully_applied({"temperature": {"state": "ok", "verification": "mismatch"}}) is False

    def test_one_unanswered_target_spoils_it(self):
        from backend.app.services.zigbee.reporting_apply import fully_applied

        assert (
            fully_applied(
                {
                    "temperature": {"state": "ok", "verification": "verified"},
                    "battery": {"state": "unanswered", "verification": "not-checked"},
                }
            )
            is False
        )

    def test_a_refusal_spoils_it(self):
        from backend.app.services.zigbee.reporting_apply import fully_applied

        assert fully_applied({"temperature": {"state": "refused", "verification": "not-checked"}}) is False

    def test_nothing_applied_is_not_a_complete_apply(self):
        """An empty result means no target was reached at all — a device with no
        clusters we know, or a radio that went away mid-loop."""
        from backend.app.services.zigbee.reporting_apply import fully_applied

        assert fully_applied({}) is False
