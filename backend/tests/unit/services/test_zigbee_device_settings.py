"""Registry defaults → global setting → this device.

Every loader is total: these are read from a pairing callback and a background
loop, where an exception is a feature that silently stops working.
"""

import json
from types import SimpleNamespace

import pytest

from backend.app.services.zigbee.devices import DeviceInfo, DeviceKind


def _info(kind=DeviceKind.SENSOR, measurements=("temperature",)):
    return DeviceInfo(
        ieee="aa:bb",
        nwk=1,
        manufacturer="X",
        model="Y",
        kind=kind,
        measurements=measurements,
        has_metering=True,
        has_electrical_measurement=True,
        reject_reason=None,
    )


def _row(**fields):
    base = {"ieee": "aa:bb", "reporting": None, "poll_seconds": None, "stale_after_seconds": None}
    return SimpleNamespace(**{**base, **fields})


class _Db:
    """Enough of AsyncSession for these loaders: a settings map and one row."""

    def __init__(self, settings=None, row=None):
        self.values = settings or {}
        self.row = row

    async def get(self, _model, _pk):
        return self.row


@pytest.fixture(autouse=True)
def patched(monkeypatch):
    async def fake_get_setting(db, key):
        return db.values.get(key)

    monkeypatch.setattr("backend.app.services.zigbee.device_settings.get_setting", fake_get_setting)


@pytest.mark.asyncio
async def test_with_nothing_stored_the_registry_defaults_win():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    resolved = await resolve_reporting(_Db(), _info())

    assert resolved["temperature"]["min_interval"] == 30
    assert resolved["temperature"]["max_interval"] == 900


@pytest.mark.asyncio
async def test_the_global_setting_overrides_the_registry():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    db = _Db({"zigbee_sensor_reporting": json.dumps({"temperature": {"max_interval": 600}})})
    resolved = await resolve_reporting(db, _info())

    assert resolved["temperature"]["max_interval"] == 600
    assert resolved["temperature"]["min_interval"] == 30, "untouched fields keep the layer beneath"


@pytest.mark.asyncio
async def test_a_device_override_beats_the_global_setting():
    """This is the layer the cycle exists for. A farm-wide default must not
    reach into a device somebody configured deliberately."""
    from backend.app.services.zigbee.device_settings import resolve_reporting

    db = _Db(
        {"zigbee_sensor_reporting": json.dumps({"temperature": {"max_interval": 600}})},
        row=_row(reporting={"temperature": {"max_interval": 120}}),
    )
    resolved = await resolve_reporting(db, _info())

    assert resolved["temperature"]["max_interval"] == 120


@pytest.mark.asyncio
async def test_a_device_override_of_one_field_leaves_the_others_to_the_layers_beneath():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    db = _Db(
        {"zigbee_sensor_reporting": json.dumps({"temperature": {"min_interval": 45}})},
        row=_row(reporting={"temperature": {"max_interval": 120}}),
    )
    resolved = await resolve_reporting(db, _info())

    assert resolved["temperature"] == {"min_interval": 45, "max_interval": 120, "reportable_change": 0.1}


@pytest.mark.asyncio
async def test_a_plug_resolves_against_plug_targets():
    """The same three layers, the same code, a different vocabulary — which is
    the whole point of having one."""
    from backend.app.services.zigbee.device_settings import resolve_reporting

    resolved = await resolve_reporting(_Db(), _info(DeviceKind.PLUG, measurements=()))

    assert set(resolved) == {"state", "power", "energy"}
    assert resolved["energy"]["min_interval"] == 30


@pytest.mark.asyncio
async def test_a_plug_override_is_read_from_the_same_column():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    db = _Db(row=_row(reporting={"power": {"reportable_change": 5}}))
    resolved = await resolve_reporting(db, _info(DeviceKind.PLUG, measurements=()))

    assert resolved["power"]["reportable_change"] == 5


@pytest.mark.asyncio
async def test_corrupt_json_at_any_layer_falls_through_instead_of_raising():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    resolved = await resolve_reporting(_Db({"zigbee_sensor_reporting": "{not json"}), _info())

    assert resolved["temperature"]["min_interval"] == 30


@pytest.mark.asyncio
async def test_an_unknown_key_in_a_device_override_is_ignored():
    """A target that no longer exists — a measurement dropped from the registry,
    or an IEEE that now carries a different model."""
    from backend.app.services.zigbee.device_settings import resolve_reporting

    db = _Db(row=_row(reporting={"radiation": {"min_interval": 5}}))
    resolved = await resolve_reporting(db, _info())

    assert "radiation" not in resolved


@pytest.mark.asyncio
async def test_a_non_numeric_field_keeps_the_layer_beneath():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    db = _Db(row=_row(reporting={"temperature": {"min_interval": "often"}}))
    resolved = await resolve_reporting(db, _info())

    assert resolved["temperature"]["min_interval"] == 30


@pytest.mark.asyncio
async def test_a_device_with_no_targets_resolves_to_nothing():
    from backend.app.services.zigbee.device_settings import resolve_reporting

    assert await resolve_reporting(_Db(), _info(DeviceKind.UNSUPPORTED, measurements=())) == {}


class TestPollInterval:
    @pytest.mark.asyncio
    async def test_the_default_is_thirty_seconds(self):
        from backend.app.services.zigbee.device_settings import resolve_poll_seconds

        assert await resolve_poll_seconds(_Db(), "aa:bb") == 30

    @pytest.mark.asyncio
    async def test_the_global_setting_is_honoured(self):
        from backend.app.services.zigbee.device_settings import resolve_poll_seconds

        assert await resolve_poll_seconds(_Db({"zigbee_sensor_poll_seconds": "120"}), "aa:bb") == 120

    @pytest.mark.asyncio
    async def test_a_device_override_beats_the_global_setting(self):
        from backend.app.services.zigbee.device_settings import resolve_poll_seconds

        db = _Db({"zigbee_sensor_poll_seconds": "120"}, row=_row(poll_seconds=90))

        assert await resolve_poll_seconds(db, "aa:bb") == 90

    @pytest.mark.asyncio
    async def test_nonsense_falls_back_rather_than_breaking_the_loop(self):
        from backend.app.services.zigbee.device_settings import resolve_poll_seconds

        assert await resolve_poll_seconds(_Db({"zigbee_sensor_poll_seconds": "soon"}), "aa:bb") == 30
        assert await resolve_poll_seconds(_Db({"zigbee_sensor_poll_seconds": "0"}), "aa:bb") == 30


class TestStalenessDefaultsPerMechanism:
    """One question — after how many seconds do we stop trusting the last value
    — with the default computed from whatever actually keeps that device fresh.

    Unifying this through a multiplier was rejected: it gives a polled plug
    60–90 s instead of 120 and therefore SHORTENS the time to "unreachable",
    and a plug wrongly marked offline is worse than one marked late, because
    that is the reading people act on.
    """

    @pytest.mark.asyncio
    async def test_a_polled_device_keeps_the_existing_two_minutes(self):
        from backend.app.services.zigbee.device_settings import resolve_stale_after_seconds

        assert await resolve_stale_after_seconds(_Db(), "aa:bb", polled=True, max_interval=900) == 120

    @pytest.mark.asyncio
    async def test_a_reporting_device_derives_from_its_own_max_interval(self):
        from backend.app.services.zigbee.device_settings import resolve_stale_after_seconds

        assert await resolve_stale_after_seconds(_Db(), "aa:bb", polled=False, max_interval=900) == 1800

    @pytest.mark.asyncio
    async def test_a_sleeper_told_to_report_rarely_is_given_longer_before_it_is_doubted(self):
        """The derivation is the point: changing how often a device speaks must
        move the threshold with it, or a slower interval reads as a fault."""
        from backend.app.services.zigbee.device_settings import resolve_stale_after_seconds

        assert await resolve_stale_after_seconds(_Db(), "aa:bb", polled=False, max_interval=10800) == 21600

    @pytest.mark.asyncio
    async def test_a_device_override_wins_over_both(self):
        from backend.app.services.zigbee.device_settings import resolve_stale_after_seconds

        db = _Db(row=_row(stale_after_seconds=45))

        assert await resolve_stale_after_seconds(db, "aa:bb", polled=True, max_interval=900) == 45
