"""``lights_report`` → ``has_chamber_light`` + ``on_lights_report``.

The camera-light lease (services/camera_light) needs two things from the MQTT
client: whether the printer HAS a light it can switch, and every later switch
of it with its direction. The first report of a connection is a sync — the
printer telling us where the light stands — and must not be reported onward as
a switch, or a reconnect mid-stream would read as the operator taking the light
over and the lease would never switch it off again.
"""

from unittest.mock import Mock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def client():
    reports = Mock()
    c = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
        on_lights_report=reports,
    )
    c.state.connected = True
    return c, reports


def _lights(mode: str, node: str = "chamber_light") -> dict:
    return {"lights_report": [{"node": node, "mode": mode}]}


def test_a_printer_that_never_reported_a_chamber_light_has_none(client):
    c, reports = client
    assert c.state.has_chamber_light is False
    c._update_state(_lights("on", node="work_light"))
    assert c.state.has_chamber_light is False
    reports.assert_not_called()


def test_the_first_report_is_a_sync_not_a_switch(client):
    c, reports = client
    c._update_state(_lights("on"))
    assert c.state.has_chamber_light is True
    assert c.state.chamber_light is True
    reports.assert_not_called()


def test_a_later_change_is_reported_with_its_direction(client):
    c, reports = client
    c._update_state(_lights("on"))
    c._update_state(_lights("off"))
    reports.assert_called_once_with(False)
    c._update_state(_lights("on"))
    assert reports.call_args_list[-1].args == (True,)


def test_the_same_state_again_is_not_a_switch(client):
    c, reports = client
    c._update_state(_lights("off"))
    c._update_state(_lights("off"))
    c._update_state(_lights("off"))
    reports.assert_not_called()


def test_a_failing_listener_does_not_break_the_parser(client):
    c, reports = client
    reports.side_effect = RuntimeError("listener gone")
    c._update_state(_lights("on"))
    c._update_state(_lights("off"))
    assert c.state.chamber_light is False
