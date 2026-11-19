"""Smart plugs and Home Assistant: four defects of the 1.3.0 upstream audit (G15).

1. One Tasmota plug per printer holds on EVERY edit, and a printer that already holds two answers
   with the refusal, not a 500.
2. A stored row a write would refuse today is listed and marked; it never fails the whole answer,
   and no driver is asked to act on it.
3. The Home Assistant scripts linked to a printer run when BamDude itself switches the plug that
   feeds it — through the one seam every switch site calls.
4. The Home Assistant connection test names why it failed, as a code of a closed set.
"""

import logging
import ssl
from datetime import datetime as real_datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.app.core import database
from backend.app.services import homeassistant as ha_module, smart_plug_manager as plug_module
from backend.app.services.homeassistant import HA_TEST_FAILURES, HomeAssistantService
from backend.app.services.smart_plug_manager import plug_switched, smart_plug_manager

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- 1. one Tasmota plug


class TestOneTasmotaPlugPerPrinter:
    async def test_changing_only_the_type_cannot_make_a_second_one(
        self, async_client, smart_plug_factory, printer_factory
    ):
        printer = await printer_factory()
        await smart_plug_factory(name="Mains", printer_id=printer.id)
        other = await smart_plug_factory(name="Lamp", plug_type="homeassistant", printer_id=printer.id)

        response = await async_client.patch(
            f"/api/v1/smart-plugs/{other.id}", json={"plug_type": "tasmota", "ip_address": "192.168.1.101"}
        )

        assert response.status_code == 400, response.text
        listed = (await async_client.get(f"/api/v1/smart-plugs/{other.id}")).json()
        assert listed["plug_type"] == "homeassistant"

    async def test_moving_a_tasmota_plug_onto_a_printer_that_has_one_is_refused(
        self, async_client, smart_plug_factory, printer_factory
    ):
        printer = await printer_factory()
        await smart_plug_factory(name="Mains", printer_id=printer.id)
        loose = await smart_plug_factory(name="Loose", ip_address="192.168.1.102")

        response = await async_client.patch(f"/api/v1/smart-plugs/{loose.id}", json={"printer_id": printer.id})

        assert response.status_code == 400, response.text

    async def test_a_printer_that_already_holds_two_refuses_a_third_and_lets_them_be_edited(
        self, async_client, smart_plug_factory, printer_factory
    ):
        printer = await printer_factory()
        first = await smart_plug_factory(name="First", printer_id=printer.id)
        await smart_plug_factory(name="Second", printer_id=printer.id, ip_address="192.168.1.101")
        loose = await smart_plug_factory(name="Loose", ip_address="192.168.1.102")

        created = await async_client.post(
            "/api/v1/smart-plugs/",
            json={"name": "Third", "plug_type": "tasmota", "ip_address": "192.168.1.103", "printer_id": printer.id},
        )
        moved = await async_client.patch(f"/api/v1/smart-plugs/{loose.id}", json={"printer_id": printer.id})
        renamed = await async_client.patch(
            f"/api/v1/smart-plugs/{first.id}", json={"name": "Renamed", "printer_id": printer.id}
        )

        assert created.status_code == 400, created.text
        assert moved.status_code == 400, moved.text
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Renamed"


# --------------------------------------------------------------------------- 2. an unreadable row


class TestARowThatNeedsCorrecting:
    async def test_it_is_listed_and_marked_everywhere_it_is_read(
        self, async_client, smart_plug_factory, printer_factory
    ):
        printer = await printer_factory()
        broken = await smart_plug_factory(
            name="No entity", plug_type="homeassistant", ha_entity_id=None, printer_id=printer.id
        )
        healthy = await smart_plug_factory(name="Healthy")

        listed = await async_client.get("/api/v1/smart-plugs/")
        batch = await async_client.get(f"/api/v1/smart-plugs/by-printer/batch?ids={printer.id}")
        main = await async_client.get(f"/api/v1/smart-plugs/by-printer/{printer.id}")
        scripts = await async_client.get(f"/api/v1/smart-plugs/by-printer/{printer.id}/scripts")
        single = await async_client.get(f"/api/v1/smart-plugs/{broken.id}")

        for response in (listed, batch, main, scripts, single):
            assert response.status_code == 200, response.text
        by_id = {row["id"]: row for row in listed.json()}
        assert by_id[broken.id]["needs_attention"] is True
        assert by_id[broken.id]["problems"] == ["ha_entity_required"]
        assert (by_id[healthy.id]["needs_attention"], by_id[healthy.id]["problems"]) == (False, [])
        assert batch.json()[str(printer.id)]["main"]["problems"] == ["ha_entity_required"]
        assert main.json()["needs_attention"] is True
        assert single.json()["problems"] == ["ha_entity_required"]

    async def test_a_stored_value_out_of_range_is_shown_too(self, async_client, smart_plug_factory):
        odd = await smart_plug_factory(name="Odd", off_delay_minutes=999)

        listed = await async_client.get("/api/v1/smart-plugs/")

        assert listed.status_code == 200, listed.text
        row = next(row for row in listed.json() if row["id"] == odd.id)
        assert row["needs_attention"] is True and row["problems"] == ["invalid_value"]
        assert row["off_delay_minutes"] == 999

    async def test_no_driver_is_asked_to_act_on_it(
        self, async_client, smart_plug_factory, mock_homeassistant_service, mock_tasmota_service
    ):
        broken = await smart_plug_factory(name="No entity", plug_type="homeassistant", ha_entity_id=None)
        no_address = await smart_plug_factory(name="No address", ip_address=None)

        for plug in (broken, no_address):
            for action in ("on", "off", "toggle"):
                response = await async_client.post(f"/api/v1/smart-plugs/{plug.id}/control", json={"action": action})
                assert response.status_code == 409, response.text
            status = await async_client.get(f"/api/v1/smart-plugs/{plug.id}/status")
            assert status.status_code == 200, status.text
            assert (status.json()["reachable"], status.json()["state"]) == (False, None)

        for service in (mock_homeassistant_service, mock_tasmota_service):
            for call in (service.turn_on, service.turn_off, service.toggle, service.get_status, service.get_energy):
                call.assert_not_awaited()

    async def test_it_can_be_corrected(self, async_client, smart_plug_factory):
        broken = await smart_plug_factory(name="No entity", plug_type="homeassistant", ha_entity_id=None)

        response = await async_client.patch(f"/api/v1/smart-plugs/{broken.id}", json={"ha_entity_id": "switch.fixed"})

        assert response.status_code == 200, response.text
        assert (response.json()["needs_attention"], response.json()["problems"]) == (False, [])

    async def test_a_write_still_refuses_what_a_read_only_marks(self, async_client):
        response = await async_client.post("/api/v1/smart-plugs/", json={"name": "Bad", "plug_type": "homeassistant"})

        assert response.status_code == 422, response.text


# --------------------------------------------------------------------------- 3. linked scripts


@pytest.fixture
def quiet_printers(monkeypatch):
    """No printer is connected in these tests: nothing prints, and the offline mark is a no-op."""
    monkeypatch.setattr(plug_module.printer_manager, "is_print_active", lambda printer_id: False)
    monkeypatch.setattr(plug_module.printer_manager, "mark_printer_offline", lambda printer_id: None)
    plug_module._recent_automatic_script_runs.clear()
    yield
    plug_module._recent_automatic_script_runs.clear()


@pytest.fixture
async def farm(smart_plug_factory, printer_factory, mock_tasmota_service, mock_homeassistant_service, quiet_printers):
    """A printer with its mains plug, an "off" script and an "on" script."""
    printer = await printer_factory()
    mains = await smart_plug_factory(name="Mains", printer_id=printer.id)

    async def script(name, **flags):
        values = {"auto_on": False, "auto_off": False} | flags
        return await smart_plug_factory(
            name=name, plug_type="homeassistant", ha_entity_id=f"script.{name}", printer_id=printer.id, **values
        )

    off_script = await script("shutdown", auto_off=True)
    on_script = await script("wake", auto_on=True)

    def ran() -> list[str]:
        return [call.args[0].ha_entity_id for call in mock_homeassistant_service.turn_on.await_args_list]

    return type(
        "Farm",
        (),
        {
            "printer": printer,
            "mains": mains,
            "off_script": off_script,
            "on_script": on_script,
            "script": staticmethod(script),
            "ran": staticmethod(ran),
            "ha": mock_homeassistant_service,
            "tasmota": mock_tasmota_service,
        },
    )


class TestLinkedScriptsOnAutomaticSwitch:
    async def test_the_plug_that_feeds_the_printer_runs_them_by_direction(self, farm):
        await plug_switched(farm.mains, False, scripts="automatic")
        assert farm.ran() == ["script.shutdown"]

        await plug_switched(farm.mains, True, scripts="automatic")
        assert farm.ran() == ["script.shutdown", "script.wake"]

    async def test_two_plugs_feeding_one_printer_run_them_once(self, farm, smart_plug_factory):
        second = await smart_plug_factory(name="Second feed", printer_id=farm.printer.id, ip_address="192.168.1.101")

        await plug_switched(farm.mains, False, scripts="automatic")
        await plug_switched(second, False, scripts="automatic")

        assert farm.ran() == ["script.shutdown"]

    async def test_an_accessory_is_not_the_printer_turning_off(self, farm, smart_plug_factory):
        filter_plug = await smart_plug_factory(
            name="Filter", printer_id=farm.printer.id, ip_address="192.168.1.101", controls_printer_power=False
        )

        await plug_switched(filter_plug, False, scripts="automatic")

        assert farm.ran() == []

    async def test_a_disabled_script_is_not_run(self, farm, db_session):
        farm.off_script.enabled = False
        await db_session.commit()

        await plug_switched(farm.mains, False, scripts="automatic")
        await plug_switched(farm.mains, False, scripts="manual", db=db_session)

        assert farm.ran() == []

    async def test_a_stale_caller_runs_none(self, farm):
        await plug_switched(farm.mains, False, scripts="automatic", may_run=lambda: False)

        assert farm.ran() == []
        assert plug_module._recent_automatic_script_runs == {}

    async def test_an_automatic_on_stays_behind_the_crash_start_pause(self, farm, monkeypatch):
        monkeypatch.setattr(plug_module.farm_startup_guard, "allowed", lambda: False)

        await plug_switched(farm.mains, True, scripts="automatic")
        await plug_switched(farm.mains, False, scripts="automatic")

        assert farm.ran() == ["script.shutdown"]

    async def test_a_script_that_fails_does_not_reach_the_switch(self, farm):
        farm.ha.turn_on.side_effect = RuntimeError("boom")

        await plug_switched(farm.mains, False, scripts="automatic")

        assert farm.ha.turn_on.await_count == 1

    async def test_the_schedule_runs_them_both_ways(self, farm, db_session, monkeypatch):
        class At(real_datetime):
            moment = real_datetime(2026, 11, 18, 22, 0, 30)

            @classmethod
            def now(cls, tz=None):
                return cls.moment if tz is None else real_datetime.now(tz)

        monkeypatch.setattr(plug_module, "datetime", At)
        farm.mains.schedule_enabled = True
        farm.mains.schedule_off_time = "22:00"
        farm.mains.schedule_on_time = "22:05"
        await db_session.commit()

        await smart_plug_manager._check_schedules()
        assert farm.ran() == ["script.shutdown"]
        farm.tasmota.turn_off.assert_awaited_once()

        At.moment = real_datetime(2026, 11, 18, 22, 5, 30)
        await smart_plug_manager._check_schedules()
        assert farm.ran() == ["script.shutdown", "script.wake"]

    async def test_the_delayed_off_runs_them(self, farm):
        await smart_plug_manager._delayed_off(
            farm.mains.id, "tasmota", farm.mains.ip_address, None, None, None, farm.printer.id, 0
        )

        farm.tasmota.turn_off.assert_awaited_once()
        assert farm.ran() == ["script.shutdown"]

    async def test_the_delayed_off_of_an_accessory_runs_none(self, farm):
        await smart_plug_manager._delayed_off(
            farm.mains.id,
            "tasmota",
            farm.mains.ip_address,
            None,
            None,
            None,
            farm.printer.id,
            0,
            controls_printer_power=False,
        )

        farm.tasmota.turn_off.assert_awaited_once()
        assert farm.ran() == []

    async def test_the_temperature_off_runs_them(self, farm, monkeypatch):
        status = type("Status", (), {"temperatures": {"nozzle": 30}, "state": "IDLE"})()
        monkeypatch.setattr(plug_module.printer_manager, "get_status", lambda printer_id: status)

        await smart_plug_manager._temp_based_off(
            farm.mains.id, "tasmota", farm.mains.ip_address, None, None, None, farm.printer.id, 70
        )

        farm.tasmota.turn_off.assert_awaited_once()
        assert farm.ran() == ["script.shutdown"]

    async def test_an_auto_off_resumed_after_a_restart_runs_them(self, farm, db_session):
        farm.mains.auto_off_pending = True
        farm.mains.auto_off_pending_since = None
        await db_session.commit()

        await smart_plug_manager.resume_pending_auto_offs()

        farm.tasmota.turn_off.assert_awaited_once()
        assert farm.ran() == ["script.shutdown"]

    async def test_failure_detection_runs_them_once_and_never_stops_a_script(
        self, farm, smart_plug_factory, monkeypatch
    ):
        from backend.app.services import obico_actions

        monkeypatch.setattr(obico_actions, "async_session", database.async_session)
        await smart_plug_factory(name="Second feed", printer_id=farm.printer.id, ip_address="192.168.1.101")

        await obico_actions._turn_off_linked_plugs(farm.printer.id)

        assert farm.tasmota.turn_off.await_count == 2
        farm.ha.turn_off.assert_not_awaited()  # script/turn_off STOPS a running script
        assert farm.ran() == ["script.shutdown"]

    async def test_print_start_runs_the_on_scripts_once(self, farm, db_session):
        await smart_plug_manager.on_print_start(farm.printer.id, db_session)

        assert farm.ran() == ["script.wake"]


class TestLinkedScriptsOnManualSwitch:
    async def test_an_explicit_off_runs_the_off_scripts(self, async_client, farm):
        response = await async_client.post(f"/api/v1/smart-plugs/{farm.mains.id}/control", json={"action": "off"})

        assert response.status_code == 200, response.text
        assert farm.ran() == ["script.shutdown"]

    async def test_a_disabled_script_is_not_run_by_hand_either(self, async_client, farm, db_session):
        farm.off_script.enabled = False
        await db_session.commit()

        response = await async_client.post(f"/api/v1/smart-plugs/{farm.mains.id}/control", json={"action": "off"})

        assert response.status_code == 200, response.text
        assert farm.ran() == []

    async def test_a_toggle_runs_none(self, async_client, farm):
        response = await async_client.post(f"/api/v1/smart-plugs/{farm.mains.id}/control", json={"action": "toggle"})

        assert response.status_code == 200, response.text
        assert farm.ran() == []

    async def test_running_a_script_by_its_button_runs_only_that_script(self, async_client, farm):
        response = await async_client.post(f"/api/v1/smart-plugs/{farm.on_script.id}/control", json={"action": "on"})

        assert response.status_code == 200, response.text
        assert farm.ran() == ["script.wake"]


def test_the_script_dedup_leaves_with_its_printer():
    from backend.app.services import printer_deletion

    assert ("backend.app.services.smart_plug_manager", "_recent_automatic_script_runs") in printer_deletion._ID_KEYED
    registry = {(7, "OFF"): 1.0, (8, "OFF"): 2.0}
    assert printer_deletion._take(registry, 7) == {(7, "OFF"): 1.0}
    assert registry == {(8, "OFF"): 2.0}


# --------------------------------------------------------------------------- 4. the connection test

SAFE_URL = "http://192.168.1.10:8123"
TOKEN = "CANARY_TOKEN_VALUE"


def _answers(monkeypatch, handler):
    """Every client the service builds talks to ``handler`` instead of the network."""
    real_client = httpx.AsyncClient
    seen = []

    def responder(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        ha_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(responder), **kwargs),
    )
    return seen


def _tls(error: ssl.SSLError):
    def handler(request):
        try:
            raise error
        except ssl.SSLError as cause:
            raise httpx.ConnectError("connect failed") from cause

    return handler


def _cert_error(verify_message: str) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] {verify_message}")
    error.verify_message = verify_message
    return error


def _raises(error):
    def handler(request):
        raise error

    return handler


FAILURES = [
    (lambda request: httpx.Response(401), "unauthorized"),
    (lambda request: httpx.Response(403), "unauthorized"),
    (lambda request: httpx.Response(302, headers={"Location": "https://elsewhere.example/"}), "redirect"),
    (lambda request: httpx.Response(404), "unexpected_answer"),
    (lambda request: httpx.Response(200, text="<html>router</html>"), "unexpected_answer"),
    (lambda request: httpx.Response(200, json=["not", "an", "api"]), "unexpected_answer"),
    (_raises(httpx.ConnectError("refused")), "unreachable"),
    (_raises(httpx.ConnectTimeout("slow")), "timeout"),
    (_raises(httpx.ReadTimeout("slow")), "timeout"),
    (_tls(_cert_error("self-signed certificate")), "certificate_untrusted"),
    (_tls(_cert_error("Hostname mismatch, certificate is not valid for '192.168.1.10'.")), "certificate_mismatch"),
    (_tls(_cert_error("certificate has expired")), "certificate_expired"),
    (_tls(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")), "not_https"),
]


class TestHomeAssistantConnectionTest:
    @pytest.mark.parametrize(
        "handler,reason", FAILURES, ids=[f"{i}-{reason}" for i, (_, reason) in enumerate(FAILURES)]
    )
    async def test_a_failure_is_named(self, monkeypatch, caplog, handler, reason):
        _answers(monkeypatch, handler)

        with caplog.at_level(logging.WARNING, logger=ha_module.logger.name):
            result = await HomeAssistantService().test_connection(SAFE_URL, TOKEN)

        assert result == {"success": False, "message": None, "reason": reason, "error": HA_TEST_FAILURES[reason]}
        assert f"reason={reason}" in caplog.text
        assert TOKEN not in caplog.text

    async def test_an_address_outside_the_policy_is_named_without_a_request(self, monkeypatch):
        seen = _answers(monkeypatch, lambda request: httpx.Response(200, json={"message": "API running."}))

        result = await HomeAssistantService().test_connection("ftp://192.168.1.10/", TOKEN)

        assert result["reason"] == "invalid_url"
        assert seen == []

    async def test_success_answers_home_assistants_own_message_and_follows_no_redirect(self, monkeypatch):
        seen = _answers(monkeypatch, lambda request: httpx.Response(200, json={"message": "API running."}))

        result = await HomeAssistantService().test_connection(SAFE_URL, TOKEN)

        assert result == {"success": True, "message": "API running.", "reason": None, "error": None}
        assert [str(request.url) for request in seen] == [f"{SAFE_URL}/api/"]

    async def test_the_log_names_the_host_and_nothing_secret(self, monkeypatch, caplog):
        _answers(monkeypatch, lambda request: httpx.Response(401))

        with caplog.at_level(logging.WARNING, logger=ha_module.logger.name):
            await HomeAssistantService().test_connection("http://admin:CANARY_PW@192.168.1.10:8123/CANARY_PATH", TOKEN)

        assert "192.168.1.10" in caplog.text
        for secret in ("CANARY_PW", "CANARY_PATH", TOKEN, "admin"):
            assert secret not in caplog.text

    async def test_no_sentence_repeats_a_value(self):
        assert all("{" not in sentence and "%" not in sentence for sentence in HA_TEST_FAILURES.values())

    async def test_the_route_answers_the_code_and_its_fixed_sentence(self, async_client, monkeypatch):
        from backend.app.services.homeassistant import homeassistant_service

        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.setattr(
            homeassistant_service,
            "test_connection",
            AsyncMock(return_value={"success": False, "reason": "certificate_expired", "error": "CANARY_TEXT"}),
        )

        response = await async_client.post(
            "/api/v1/smart-plugs/ha/test-connection", json={"url": SAFE_URL, "token": TOKEN}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "success": False,
            "message": None,
            "reason": "certificate_expired",
            "error": HA_TEST_FAILURES["certificate_expired"],
        }

    async def test_the_route_never_passes_on_a_reason_outside_the_set(self, async_client, monkeypatch):
        from backend.app.services.homeassistant import homeassistant_service

        monkeypatch.delenv("HA_URL", raising=False)
        monkeypatch.delenv("HA_TOKEN", raising=False)
        monkeypatch.setattr(
            homeassistant_service,
            "test_connection",
            AsyncMock(return_value={"success": False, "reason": "CANARY_REASON", "error": "CANARY_TEXT"}),
        )

        response = await async_client.post(
            "/api/v1/smart-plugs/ha/test-connection", json={"url": SAFE_URL, "token": TOKEN}
        )

        assert response.json()["reason"] == "unexpected_answer"
        assert "CANARY" not in response.text
