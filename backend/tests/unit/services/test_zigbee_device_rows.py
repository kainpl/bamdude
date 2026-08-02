"""The row appears at pairing, because settings need somewhere to live from the
moment a device is on the mesh — not from the moment somebody adopts it.

A Zigbee plug is paired first and added as a device afterwards. Between those
two moments it is already being configured, and a table keyed by plug id could
not hold anything about it.
"""

import pytest

from backend.app.services.zigbee.devices import DeviceInfo, DeviceKind


def _info(ieee="aa:bb", kind=DeviceKind.SENSOR, manufacturer="SONOFF", model="SNZB-02DR2"):
    return DeviceInfo(
        ieee=ieee,
        nwk=1,
        manufacturer=manufacturer,
        model=model,
        kind=kind,
        measurements=("temperature",),
        cluster_ids=frozenset({0x0402}),
        has_metering=False,
        has_electrical_measurement=False,
        reject_reason=None,
    )


@pytest.mark.asyncio
async def test_pairing_creates_a_row_with_the_hardware_name(db_session):
    from backend.app.services.zigbee.device_settings import upsert_device_row

    row = await upsert_device_row(db_session, _info())

    assert row.ieee == "aa:bb"
    assert row.kind == "sensor"
    assert row.name == "SONOFF SNZB-02DR2"


@pytest.mark.asyncio
async def test_the_ieee_is_stored_in_one_case_however_it_arrives(db_session):
    """zigpy renders an EUI64 lower-case; a route takes whatever a caller typed.
    Two rows for one device would each hold half the settings."""
    from backend.app.services.zigbee.device_settings import load_device_row, upsert_device_row

    await upsert_device_row(db_session, _info(ieee="AA:BB"))

    assert await load_device_row(db_session, "aa:bb") is not None
    assert await load_device_row(db_session, "AA:BB") is not None


@pytest.mark.asyncio
async def test_a_device_with_no_name_to_give_does_not_get_an_empty_one(db_session):
    from backend.app.services.zigbee.device_settings import upsert_device_row

    row = await upsert_device_row(db_session, _info(manufacturer=None, model=None))

    assert row.name is None


@pytest.mark.asyncio
async def test_re_pairing_keeps_the_operators_settings(db_session):
    """Re-pairing a device that walked out of range must not reset what somebody
    configured on it."""
    from backend.app.services.zigbee.device_settings import save_overrides, upsert_device_row

    await upsert_device_row(db_session, _info())
    await save_overrides(db_session, "aa:bb", poll_seconds=90, reporting={"temperature": {"max_interval": 120}})
    row = await upsert_device_row(db_session, _info())

    assert row.poll_seconds == 90
    assert row.reporting == {"temperature": {"max_interval": 120}}


@pytest.mark.asyncio
async def test_re_pairing_refreshes_what_the_radio_now_says(db_session):
    """The same IEEE can carry a different model after a device is replaced."""
    from backend.app.services.zigbee.device_settings import upsert_device_row

    await upsert_device_row(db_session, _info(model="SNZB-02DR2"))
    row = await upsert_device_row(db_session, _info(model="SNZB-02P"))

    assert row.name == "SONOFF SNZB-02P"


@pytest.mark.asyncio
async def test_saving_settings_for_a_device_that_never_paired_says_so(db_session):
    from backend.app.services.zigbee.device_settings import save_overrides

    assert await save_overrides(db_session, "ff:ff", poll_seconds=60) is False


class TestReconcile:
    """The migration has no radio, so devices paired before the table existed
    are given rows on the first startup after it lands."""

    @pytest.mark.asyncio
    async def test_it_adds_rows_for_devices_paired_before_this_existed(self, db_session):
        from backend.app.models.zigbee_device import ZigbeeDevice
        from backend.app.services.zigbee.device_settings import reconcile_device_rows

        added = await reconcile_device_rows([_info("aa:bb"), _info("cc:dd")], db_session)

        assert added == 2
        assert await db_session.get(ZigbeeDevice, "cc:dd") is not None

    @pytest.mark.asyncio
    async def test_it_is_idempotent_across_restarts(self, db_session):
        from backend.app.services.zigbee.device_settings import reconcile_device_rows

        await reconcile_device_rows([_info("aa:bb")], db_session)

        assert await reconcile_device_rows([_info("aa:bb")], db_session) == 0

    @pytest.mark.asyncio
    async def test_it_does_not_touch_settings_on_a_row_that_exists(self, db_session):
        """Every boot runs this. Refreshing a row here would wipe an operator's
        settings once per restart, which nothing would report."""
        from backend.app.services.zigbee.device_settings import (
            load_device_row,
            reconcile_device_rows,
            save_overrides,
            upsert_device_row,
        )

        await upsert_device_row(db_session, _info())
        await save_overrides(db_session, "aa:bb", stale_after_seconds=4000)
        await reconcile_device_rows([_info("aa:bb")], db_session)

        row = await load_device_row(db_session, "aa:bb")
        assert row.stale_after_seconds == 4000

    @pytest.mark.asyncio
    async def test_the_coordinator_is_not_given_a_row(self, db_session):
        """It is our own radio, not a device anybody configures."""
        from backend.app.services.zigbee.device_settings import reconcile_device_rows

        assert await reconcile_device_rows([_info("00:00", kind=DeviceKind.COORDINATOR)], db_session) == 0

    @pytest.mark.asyncio
    async def test_an_unsupported_device_is_not_given_a_row(self, db_session):
        """BamDude can neither switch nor read it; there is nothing to store."""
        from backend.app.services.zigbee.device_settings import reconcile_device_rows

        assert await reconcile_device_rows([_info("de:ad", kind=DeviceKind.UNSUPPORTED)], db_session) == 0


class TestForgetting:
    @pytest.mark.asyncio
    async def test_unpairing_removes_the_row(self, db_session):
        from backend.app.models.zigbee_device import ZigbeeDevice
        from backend.app.services.zigbee.device_settings import forget_device_row, upsert_device_row

        await upsert_device_row(db_session, _info())
        await forget_device_row(db_session, "aa:bb")

        assert await db_session.get(ZigbeeDevice, "aa:bb") is None

    @pytest.mark.asyncio
    async def test_unpairing_takes_the_adopted_sensor_with_it(self, db_session):
        """The device is gone from the network; a row saying the farm still
        tracks it would point at nothing and could never be reached again."""
        from sqlalchemy import select

        from backend.app.models.smart_sensor import SmartSensor
        from backend.app.services.zigbee.device_settings import forget_device_row, upsert_device_row

        await upsert_device_row(db_session, _info())
        db_session.add(SmartSensor(name="Workshop", zigbee_ieee="aa:bb"))
        await db_session.commit()

        await forget_device_row(db_session, "aa:bb")

        assert (await db_session.execute(select(SmartSensor))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_forgetting_something_that_was_never_there_is_not_an_error(self, db_session):
        from backend.app.services.zigbee.device_settings import forget_device_row

        await forget_device_row(db_session, "ff:ff")
