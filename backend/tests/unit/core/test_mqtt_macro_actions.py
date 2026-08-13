"""The MQTT macro catalog: one grammar — action id + optional parameter.

The two chamber-light entries used to bake their value into the id. Keeping
that alongside a parameterized ``print_speed`` would have left the catalog
speaking two languages, and every action added later (fan, temperature) would
have had to pick one. These tests pin the single grammar and the legacy ids
that still have to resolve.
"""

from unittest.mock import MagicMock

from backend.app.core.mqtt_macro_actions import (
    MQTT_MACRO_ACTIONS,
    catalog_for_meta,
    get_action,
    resolve_action,
)
from backend.app.services.macro_executor import dispatch_mqtt_action


def _client() -> MagicMock:
    client = MagicMock()
    client.state.connected = True
    client.set_chamber_light.return_value = True
    client.set_print_speed.return_value = True
    return client


class TestCatalogShape:
    def test_the_light_is_one_action_with_a_parameter(self) -> None:
        assert "chamber_light" in MQTT_MACRO_ACTIONS
        assert "chamber_light_on" not in MQTT_MACRO_ACTIONS
        assert "chamber_light_off" not in MQTT_MACRO_ACTIONS

        spec = MQTT_MACRO_ACTIONS["chamber_light"].param
        assert spec is not None
        assert [c.value for c in spec.choices] == ["on", "off"]

    def test_print_speed_offers_the_four_bambu_levels(self) -> None:
        """BS ``DevPrintingSpeedLevel``: 1 silence, 2 normal, 3 rapid, 4 rampage.
        There is no per-model gating anywhere in BS, so the list is static."""
        spec = MQTT_MACRO_ACTIONS["print_speed"].param
        assert spec is not None
        assert [c.value for c in spec.choices] == ["1", "2", "3", "4"]
        assert spec.default == "2"

    def test_meta_carries_the_param_spec(self) -> None:
        by_id = {a["id"]: a for a in catalog_for_meta()}
        assert by_id["print_speed"]["param"]["kind"] == "choice"
        assert len(by_id["print_speed"]["param"]["choices"]) == 4


class TestLegacyIds:
    def test_an_old_light_id_resolves_to_the_new_action_and_its_value(self) -> None:
        action, param = resolve_action("chamber_light_on")
        assert action is not None and action.id == "chamber_light"
        assert param == "on"

    def test_get_action_still_answers_for_an_old_id(self) -> None:
        assert get_action("chamber_light_off") is not None

    def test_an_unknown_id_resolves_to_nothing(self) -> None:
        assert resolve_action("launch_nukes") == (None, None)


class TestValidation:
    def test_a_value_outside_the_choices_is_invalid(self) -> None:
        spec = MQTT_MACRO_ACTIONS["print_speed"].param
        assert spec.is_valid("3") is True
        assert spec.is_valid("9") is False
        assert spec.is_valid(None) is False


class TestDispatch:
    def test_speed_reaches_the_client_as_an_int(self) -> None:
        client = _client()
        ok, err = dispatch_mqtt_action(client, "print_speed", "Night mode", "1")
        assert (ok, err) == (True, "")
        client.set_print_speed.assert_called_once_with(1)

    def test_light_on_reaches_the_client_as_a_bool(self) -> None:
        client = _client()
        ok, _ = dispatch_mqtt_action(client, "chamber_light", "Lights", "on")
        assert ok is True
        client.set_chamber_light.assert_called_once_with(True)

    def test_a_legacy_id_dispatches_without_a_stored_param(self) -> None:
        """A row written before the collapse — or restored from an old backup —
        still has to work."""
        client = _client()
        ok, _ = dispatch_mqtt_action(client, "chamber_light_off", "Lights", None)
        assert ok is True
        client.set_chamber_light.assert_called_once_with(False)

    def test_a_bad_param_is_refused_before_the_client_is_touched(self) -> None:
        client = _client()
        ok, err = dispatch_mqtt_action(client, "print_speed", "Bad", "9")
        assert ok is False
        assert "parameter" in err
        client.set_print_speed.assert_not_called()
