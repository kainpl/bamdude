"""A setting that quietly did nothing is worse than one that says no.

Every refusal here is a 422 carrying a sentence an operator can act on, rather
than a silently dropped field — which would leave a number on screen that the
device is not running.
"""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from backend.tests.zigbee_fixtures import BATTERY_SENSOR_FLAGS, MAINS_DEVICE_FLAGS, fake_device

SENSOR = "aa:bb:cc:dd:ee:ff:00:11"
PLUG = "11:22:33:44:55:66:77:88"


def _plug_device(ieee=PLUG):
    """Built through the shared fixture so it carries a REAL node descriptor.

    Power class is read from that descriptor, not from a flags attribute — a
    hand-rolled stub with node_desc=None reads as a battery device, which is
    the safe default and exactly the wrong answer for a plug.
    """
    return fake_device(
        ieee,
        0x0006,
        0x0702,
        0x0B04,
        mac_capability_flags=MAINS_DEVICE_FLAGS,
        model="S60ZBTPF",
    )


@pytest.fixture
def paired(monkeypatch):
    """A battery sensor and a mains plug, both on the mesh."""
    from backend.app.services.zigbee.coordinator import zigbee_coordinator

    # Asleep, which for a battery sensor is the normal state rather than a
    # fault — and the one the save path has to answer honestly about.
    sensor = fake_device(SENSOR, 0x0402, 0x0405, 0x0001, mac_capability_flags=BATTERY_SENSOR_FLAGS, asleep=True)
    plug = _plug_device()
    monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={sensor.ieee: sensor, plug.ieee: plug}))
    return SimpleNamespace(sensor=sensor, plug=plug)


async def _pair(db_session, ieee, kind):
    from backend.app.models.zigbee_device import ZigbeeDevice

    db_session.add(ZigbeeDevice(ieee=ieee.lower(), kind=kind, name="SONOFF X"))
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reading_a_sensors_settings_lists_every_target_it_has(async_client: AsyncClient, paired, db_session):
    await _pair(db_session, SENSOR, "sensor")

    body = (await async_client.get(f"/api/v1/zigbee/devices/{SENSOR}/settings")).json()

    assert set(body["desired"]) == {"temperature", "humidity", "battery", "battery_voltage"}
    assert body["desired"]["temperature"]["min_interval"] == 30


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_plug_lists_its_own_vocabulary(async_client: AsyncClient, paired, db_session):
    await _pair(db_session, PLUG, "plug")

    body = (await async_client.get(f"/api/v1/zigbee/devices/{PLUG}/settings")).json()

    assert set(body["desired"]) == {"state", "power", "energy"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_device_that_never_paired_is_a_404(async_client: AsyncClient, paired):
    assert (await async_client.get("/api/v1/zigbee/devices/ff:ff/settings")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_after_a_restart_the_applied_state_is_unknown_not_ok(async_client: AsyncClient, paired, db_session):
    """We have not asked this device anything yet. Reporting "ok" would claim a
    configuration nobody has confirmed."""
    await _pair(db_session, SENSOR, "sensor")

    body = (await async_client.get(f"/api/v1/zigbee/devices/{SENSOR}/settings")).json()

    entry = body["applied"]["temperature"]
    assert (entry["state"], entry["verification"]) == ("unknown", "not-checked")
    assert entry["describes_desired"] is False, "an unknown outcome describes no settings, least of all these"


class TestWhatMayBeChanged:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_polling_a_battery_device_is_refused_with_a_reason(
        self, async_client: AsyncClient, paired, db_session
    ):
        """422 rather than accept-and-ignore. The decision comes from the node
        descriptor, never from the device class and never from the operator."""
        await _pair(db_session, SENSOR, "sensor")

        rsp = await async_client.put(f"/api/v1/zigbee/devices/{SENSOR}/settings", json={"poll_seconds": 60})

        assert rsp.status_code == 422
        assert "sleep" in rsp.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_mains_device_accepts_a_poll_interval(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, PLUG, "plug")

        rsp = await async_client.put(f"/api/v1/zigbee/devices/{PLUG}/settings", json={"poll_seconds": 60})

        assert rsp.status_code == 200
        assert rsp.json()["poll_seconds"] == 60

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_relay_refuses_everything_but_its_heartbeat(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, PLUG, "plug")

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{PLUG}/settings",
            json={"reporting": {"state": {"min_interval": 5}}},
        )

        assert rsp.status_code == 422
        assert "min_interval" in rsp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_relay_accepts_its_heartbeat(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, PLUG, "plug")

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{PLUG}/settings",
            json={"reporting": {"state": {"max_interval": 600}}},
        )

        assert rsp.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_quantity_the_device_does_not_have_is_refused(self, async_client: AsyncClient, paired, db_session):
        """Accepting it would store a setting that resolves to nothing and can
        never be applied — visible in the API, invisible on the device."""
        await _pair(db_session, SENSOR, "sensor")

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{SENSOR}/settings",
            json={"reporting": {"co2": {"max_interval": 600}}},
        )

        assert rsp.status_code == 422
        assert "co2" in rsp.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_backwards_interval_pair_is_refused(self, async_client: AsyncClient, paired, db_session):
        """The device would accept it and then never report: a minimum longer
        than the maximum is silence with extra steps."""
        await _pair(db_session, SENSOR, "sensor")

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{SENSOR}/settings",
            json={"reporting": {"temperature": {"min_interval": 900, "max_interval": 60}}},
        )

        assert rsp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_pair_is_checked_against_what_resolves_not_only_the_request(
        self, async_client: AsyncClient, paired, db_session
    ):
        """Lowering only the maximum below the minimum already in force.

        The request on its own looks harmless — one small number — and a check
        that reads only what was sent compares it against a default of zero and
        waves it through. The device would then accept a configuration it can
        never satisfy and simply stop reporting.
        """
        await _pair(db_session, SENSOR, "sensor")

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{SENSOR}/settings",
            json={"reporting": {"temperature": {"max_interval": 10}}},
        )

        assert rsp.status_code == 422, "the resolved minimum is 30 s"


class TestSavingWhileTheDeviceSleeps:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sleeping_device_still_saves_successfully(self, async_client: AsyncClient, paired, db_session):
        """The desired state is stored and applied at the next contact. Reported
        as a failure, this ordinary outcome reads as a broken feature."""
        await _pair(db_session, SENSOR, "sensor")

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{SENSOR}/settings",
            json={"reporting": {"temperature": {"max_interval": 600}}},
        )

        assert rsp.status_code == 200
        assert rsp.json()["applied"]["temperature"]["state"] == "unanswered"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_what_was_saved_survives_the_response(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, SENSOR, "sensor")

        await async_client.put(
            f"/api/v1/zigbee/devices/{SENSOR}/settings",
            json={"reporting": {"temperature": {"max_interval": 600}}},
        )
        body = (await async_client.get(f"/api/v1/zigbee/devices/{SENSOR}/settings")).json()

        assert body["desired"]["temperature"]["max_interval"] == 600
        assert body["desired"]["temperature"]["min_interval"] == 30, "the layers beneath still apply"


class TestAnOutcomeSaysWhichSettingsItIsAbout:
    """ "Verified" alone cannot tell a fresh confirmation from last hour's.

    Found on hardware: a sleeping sensor kept the previous outcome while the new
    configuration was still on its way, so the answer showed a green tick for a
    change that had not happened. The values ride along so the reader can tell.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_plug_that_answered_carries_what_was_confirmed(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, PLUG, "plug")

        body = (
            await async_client.put(
                f"/api/v1/zigbee/devices/{PLUG}/settings",
                json={"reporting": {"power": {"max_interval": 600}}},
            )
        ).json()

        entry = body["applied"]["power"]
        assert entry["values"]["max_interval"] == 600
        assert entry["at"], "an outcome with no time cannot be told from a stale one either"
        assert entry["describes_desired"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_outcome_that_predates_the_settings_is_not_confirmation_of_them(
        self, async_client: AsyncClient, paired, db_session
    ):
        """The plug confirms 600. The desired value then moves to 900 without a
        push -- which is what a request giving up on its five-second budget
        leaves behind, and what the sleeping sensor did on live hardware.

        The stored outcome still says ok/verified, truthfully, about 600. What
        it must not say is that it describes what is on screen now.
        """
        from backend.app.services.zigbee.device_settings import save_overrides

        await _pair(db_session, PLUG, "plug")
        await async_client.put(
            f"/api/v1/zigbee/devices/{PLUG}/settings",
            json={"reporting": {"power": {"max_interval": 600}}},
        )
        await save_overrides(db_session, PLUG, reporting={"power": {"max_interval": 900}})

        entry = (await async_client.get(f"/api/v1/zigbee/devices/{PLUG}/settings")).json()["applied"]["power"]

        assert entry["state"] == "ok", "still true, about the old value"
        assert entry["values"]["max_interval"] == 600
        assert entry["describes_desired"] is False, "a green tick for a change that has not happened"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_attempt_that_got_nowhere_is_recorded_as_that_attempt(
        self, async_client: AsyncClient, paired, db_session
    ):
        """ "We last tried 900 and heard nothing" is a different fact from "we
        last verified 600", and the operator needs the first one."""
        await _pair(db_session, PLUG, "plug")
        # Asleep rather than absent: removing the cluster would take the power
        # target out of the vocabulary entirely, and the plug falls back to
        # Metering for power anyway -- the attempt would quietly succeed there.
        paired.plug.endpoints[1].in_clusters[0x0B04].asleep = True

        body = (
            await async_client.put(
                f"/api/v1/zigbee/devices/{PLUG}/settings",
                json={"reporting": {"power": {"max_interval": 900}}},
            )
        ).json()

        entry = body["applied"]["power"]
        assert entry["state"] == "unanswered"
        assert entry["values"]["max_interval"] == 900

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_clamped_value_reaches_the_answer_and_not_only_the_log(
        self, async_client: AsyncClient, monkeypatch, db_session
    ):
        """The device says SUCCESS and stores something else. Until now that
        difference went to the log alone, so "it stored something else" could
        never say what."""
        from backend.app.services.zigbee.coordinator import zigbee_coordinator

        # Answers the read-back with a max_interval it clamped to 300.
        plug = fake_device(
            PLUG, 0x0006, 0x0702, 0x0B04, mac_capability_flags=MAINS_DEVICE_FLAGS, model="S60ZBTPF", stored=(5, 300, 1)
        )
        monkeypatch.setattr(zigbee_coordinator, "_app", SimpleNamespace(devices={plug.ieee: plug}))
        await _pair(db_session, PLUG, "plug")

        body = (
            await async_client.put(
                f"/api/v1/zigbee/devices/{PLUG}/settings",
                json={"reporting": {"power": {"max_interval": 900}}},
            )
        ).json()

        entry = body["applied"]["power"]
        assert entry["verification"] == "mismatch"
        assert entry["actual"]["max_interval"] == 300, "the number an operator needs, not a line in the log"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sleeper_shows_the_new_settings_as_not_yet_confirmed(
        self, async_client: AsyncClient, paired, db_session
    ):
        await _pair(db_session, SENSOR, "sensor")

        body = (
            await async_client.put(
                f"/api/v1/zigbee/devices/{SENSOR}/settings",
                json={"reporting": {"temperature": {"max_interval": 600}}},
            )
        ).json()

        entry = body["applied"]["temperature"]
        assert entry["state"] == "unanswered"
        assert entry["verification"] == "not-checked", "nothing was read back from a device that said nothing"


class TestClearing:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_returns_the_device_to_the_defaults(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, SENSOR, "sensor")
        await async_client.put(
            f"/api/v1/zigbee/devices/{SENSOR}/settings",
            json={"reporting": {"temperature": {"max_interval": 600}}},
        )

        assert (await async_client.delete(f"/api/v1/zigbee/devices/{SENSOR}/settings")).status_code == 200

        body = (await async_client.get(f"/api/v1/zigbee/devices/{SENSOR}/settings")).json()
        assert body["desired"]["temperature"]["max_interval"] == 900


class TestEditableIsDeclared:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_response_says_which_fields_a_relay_allows(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, PLUG, "plug")

        body = (await async_client.get(f"/api/v1/zigbee/devices/{PLUG}/settings")).json()

        assert body["editable"]["state"] == ["max_interval"]
        assert body["editable"]["power"] == ["min_interval", "max_interval", "reportable_change"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sleeper_declares_that_it_cannot_be_polled(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, SENSOR, "sensor")

        body = (await async_client.get(f"/api/v1/zigbee/devices/{SENSOR}/settings")).json()

        assert body["poll_supported"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_every_target_says_what_its_change_is_measured_in(
        self, async_client: AsyncClient, paired, db_session
    ):
        """The dialog labels the "change by" field. Hardcoding a key-to-unit map
        in the frontend would be a second copy of the measurement registry, and
        it would drift at the first new quantity."""
        await _pair(db_session, SENSOR, "sensor")

        body = (await async_client.get(f"/api/v1/zigbee/devices/{SENSOR}/settings")).json()

        assert body["units"]["temperature"] == "°C"
        assert body["units"]["humidity"] == "%"
        assert body["units"]["battery"] == "%"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_relay_has_no_unit_because_it_has_no_amount(self, async_client: AsyncClient, paired, db_session):
        await _pair(db_session, PLUG, "plug")

        body = (await async_client.get(f"/api/v1/zigbee/devices/{PLUG}/settings")).json()

        assert body["units"]["state"] == ""
        assert body["units"]["power"] == "W"
        assert body["units"]["energy"] == "kWh"


class TestAPlugsSettingsActuallyReachIt:
    """Storing without pushing is the failure this whole cycle exists to remove.

    A plug is mains-powered and awake, so there is no excuse for a saved value
    to sit in the database until the next restart while the device runs the
    defaults — and nothing on screen would say so.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_saving_re_issues_the_configuration_to_the_device(
        self, async_client: AsyncClient, paired, db_session
    ):
        await _pair(db_session, PLUG, "plug")
        em = paired.plug.endpoints[1].in_clusters[0x0B04]
        em.configured.clear()

        rsp = await async_client.put(
            f"/api/v1/zigbee/devices/{PLUG}/settings",
            json={"reporting": {"power": {"min_interval": 20, "max_interval": 600}}},
        )

        assert rsp.status_code == 200
        assert em.configured, "the device was never told"
        attribute, minimum, maximum, _change = em.configured[-1]
        assert (attribute, minimum, maximum) == (0x050B, 20, 600)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_answer_reports_what_the_plug_made_of_it(self, async_client: AsyncClient, paired, db_session):
        """An awake device answers, so "unanswered" here would mean the push
        never happened."""
        await _pair(db_session, PLUG, "plug")

        body = (
            await async_client.put(
                f"/api/v1/zigbee/devices/{PLUG}/settings",
                json={"reporting": {"power": {"max_interval": 600}}},
            )
        ).json()

        assert body["applied"]["power"]["state"] == "ok"


class TestARequestNeverWaitsOnADevice:
    """A sleeping sensor answers nothing, and four measurements times zigpy's
    own timeout is over a minute of a held connection. Six of those exhaust a
    browser's per-origin pool and freeze the tab — measured on hardware once
    already, which is why no handler here waits on a device without a budget.

    Nothing is lost by giving up: the desired state is stored before the push,
    so a device that was not reached is retried at its next contact.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_sleeping_sensor_does_not_hold_the_request(
        self, async_client: AsyncClient, paired, db_session, monkeypatch
    ):
        import asyncio

        from backend.app.api.routes import zigbee as routes

        await _pair(db_session, SENSOR, "sensor")
        monkeypatch.setattr(routes, "_APPLY_BUDGET_SECONDS", 0.2)

        async def never_answers(*_args, **_kwargs):
            await asyncio.sleep(30)
            return {}

        # Patched where it is looked up: the route imports it inside the
        # function, so patching the route module would do nothing at all.
        monkeypatch.setattr("backend.app.services.zigbee.reporting.bind_sensor", never_answers)

        rsp = await asyncio.wait_for(
            async_client.put(
                f"/api/v1/zigbee/devices/{SENSOR}/settings",
                json={"reporting": {"temperature": {"max_interval": 600}}},
            ),
            timeout=5,
        )

        assert rsp.status_code == 200
        assert rsp.json()["desired"]["temperature"]["max_interval"] == 600, "saved regardless"
