"""Which macros a layer edge crosses.

Equality would be the obvious test and the wrong one: MQTT reports get dropped,
so a print can go from layer 48 straight to 52 without ever reporting 50.
"""

import json

import backend.app.models.printer_location  # noqa: F401 — Printer relates to it by name
from backend.app.models.macro import Macro
from backend.app.models.printer import Printer
from backend.app.services.macro_matcher import LAYER_REACHED_EVENT, find_layer_macros


def _printer(model: str = "P1S") -> Printer:
    p = Printer(name="P1", ip_address="1.2.3.4", serial_number="S1", access_code="1234", model=model)
    p.swap_mode_enabled = False
    p.swap_profile = None
    return p


def _macro(layer: int | None, *, model: str = "*", enabled: bool = True, event: str = LAYER_REACHED_EVENT) -> Macro:
    m = Macro(
        name=f"at {layer}",
        printer_models=json.dumps([model]),
        swap_mode_only=False,
        swap_profile=None,
        event=event,
        action_type="mqtt_action",
        mqtt_action="print_speed",
        mqtt_action_param="1",
        delay_seconds=0,
        gcode="",
        enabled=enabled,
    )
    m.trigger_layer = layer
    return m


class TestCrossing:
    def test_the_exact_layer_fires(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 49, 50) != []

    def test_a_jump_over_the_layer_still_fires(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 48, 52) != []

    def test_a_layer_already_behind_us_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 50, 51) == []

    def test_a_layer_still_ahead_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50)], 10, 11) == []

    def test_two_macros_crossed_by_one_jump_both_fire(self) -> None:
        found = find_layer_macros(_printer(), [_macro(50), _macro(51)], 48, 52)
        assert len(found) == 2


class TestTheOrdinaryMatcherStillApplies:
    def test_a_disabled_macro_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50, enabled=False)], 49, 50) == []

    def test_a_macro_for_another_model_does_not_fire(self) -> None:
        assert find_layer_macros(_printer("A1"), [_macro(50, model="P1S")], 49, 50) == []

    def test_a_macro_on_another_event_does_not_fire(self) -> None:
        assert find_layer_macros(_printer(), [_macro(50, event="print_started")], 49, 50) == []

    def test_a_layerless_macro_on_this_event_is_skipped(self) -> None:
        """Validation forbids it, but a hand-edited row must not crash the parse."""
        assert find_layer_macros(_printer(), [_macro(None)], 49, 50) == []
