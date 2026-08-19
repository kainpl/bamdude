"""Which plug the card calls a printer's power.

Ported from upstream #2830. A printer card has one Power row: a plug name, its
draw, and the auto-off and on/off buttons. Which plug filled it was decided by
nothing — the endpoint returned the first row the database handed back that was
not a Home Assistant script, from a query with **no ORDER BY**.

For the reporter that was an enclosure exhaust fan, added before the outlet
their printer is plugged into. The card showed the fan's name with "--" for
watts, offered to switch the printer off by cutting the fan, and demoted the
metered outlet to the small button row.

⚠️ Ours was worse in one place: the "power on the offline printers" list built
its own map client-side by taking whichever plug came LAST, with no ranking at
all — and that list sends a real on/off command.
"""

from __future__ import annotations

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.api.routes.smart_plugs import (
    _can_be_switched,
    _is_script_plug,
    _main_plug_rank,
    _pick_main_plug,
    _reports_power,
)
from backend.app.models.smart_plug import SmartPlug


def _plug(plug_id: int, **fields) -> SmartPlug:
    values = {
        "id": plug_id,
        "name": f"plug{plug_id}",
        "plug_type": "tasmota",
        "printer_id": 1,
        "enabled": True,
        "controls_printer_power": True,
        "show_on_printer_card": True,
    }
    values.update(fields)
    return SmartPlug(**values)


class TestWhatCannotBeSwitched:
    def test_a_home_assistant_script(self):
        """It can be RUN, not switched — the row's on/off button is the point."""
        script = _plug(1, plug_type="homeassistant", ha_entity_id="script.printer_on")

        assert _is_script_plug(script) is True
        assert _can_be_switched(script) is False

    def test_an_mqtt_plug_is_monitor_only(self):
        """⚠️ We subscribe and never publish, so the control endpoint rejects it
        — and an MQTT plug is exactly the kind that reports watts, so without
        this it would win the power tiebreak and take the row off a plug that
        can actually be switched."""
        assert _can_be_switched(_plug(1, plug_type="mqtt")) is False

    def test_an_ordinary_switch_entity_can(self):
        assert _can_be_switched(_plug(1, plug_type="homeassistant", ha_entity_id="switch.outlet")) is True

    def test_a_tasmota_plug_can(self):
        assert _can_be_switched(_plug(1)) is True


class TestWhoReportsWatts:
    def test_home_assistant_needs_a_power_entity(self):
        assert _reports_power(_plug(1, plug_type="homeassistant", ha_power_entity="sensor.w")) is True
        assert _reports_power(_plug(2, plug_type="homeassistant", ha_entity_id="switch.x")) is False

    def test_mqtt_accepts_either_topic(self):
        assert _reports_power(_plug(1, plug_type="mqtt", mqtt_power_topic="t/power")) is True
        assert _reports_power(_plug(2, plug_type="mqtt", mqtt_topic="legacy/topic")) is True
        assert _reports_power(_plug(3, plug_type="mqtt")) is False

    def test_rest_needs_a_power_path(self):
        assert _reports_power(_plug(1, plug_type="rest", rest_power_path="$.watts")) is True
        assert _reports_power(_plug(2, plug_type="rest")) is False

    def test_tasmota_is_assumed_to(self):
        """⚠️ Approximate in both directions, and only ever a tiebreak: probing
        each plug would mean an HTTP round trip per card render."""
        assert _reports_power(_plug(1)) is True


class TestTheRanking:
    def test_the_reported_case_the_outlet_beats_the_fan(self):
        fan = _plug(1, name="exhaust fan", controls_printer_power=False, show_on_printer_card=False)
        outlet = _plug(2, name="printer outlet")

        assert _pick_main_plug([fan, outlet]) is outlet

    def test_switchability_outranks_everything(self):
        """A plug that cannot be switched cannot carry the row's buttons, however
        well it scores otherwise."""
        metered_monitor = _plug(1, plug_type="mqtt", mqtt_power_topic="t/p")
        plain_switch = _plug(2, plug_type="homeassistant", ha_entity_id="switch.outlet")

        assert _pick_main_plug([metered_monitor, plain_switch]) is plain_switch

    def test_the_power_flag_outranks_the_display_preference(self):
        """⚠️ A display preference must not hand power control to an accessory."""
        shown_accessory = _plug(1, controls_printer_power=False, show_on_printer_card=True)
        hidden_mains = _plug(2, controls_printer_power=True, show_on_printer_card=False)

        assert _pick_main_plug([shown_accessory, hidden_mains]) is hidden_mains

    def test_a_hidden_plug_is_ranked_not_filtered(self):
        """⚠️ Excluding hidden plugs outright would strip the Power row — and
        with it the on/off button — from a printer whose only plug has the flag
        off."""
        only = _plug(1, show_on_printer_card=False)

        assert _pick_main_plug([only]) is only

    def test_an_enabled_plug_beats_a_disabled_one(self):
        disabled = _plug(1, enabled=False)
        enabled = _plug(2, enabled=True)

        assert _pick_main_plug([disabled, enabled]) is enabled

    def test_metering_breaks_a_tie(self):
        blind = _plug(1, plug_type="homeassistant", ha_entity_id="switch.a")
        metered = _plug(2, plug_type="homeassistant", ha_entity_id="switch.b", ha_power_entity="sensor.w")

        assert _pick_main_plug([blind, metered]) is metered

    def test_the_lowest_id_settles_a_true_tie(self):
        """⚠️ The query had no ORDER BY at all, which on PostgreSQL means a plain
        UPDATE can move a row and silently swap which plug is the printer's."""
        assert _pick_main_plug([_plug(7), _plug(3), _plug(5)]).id == 3

    def test_the_order_of_the_input_never_decides(self):
        plugs = [_plug(1, controls_printer_power=False), _plug(2)]

        assert _pick_main_plug(plugs) is _pick_main_plug(list(reversed(plugs)))

    def test_a_printer_with_no_plugs(self):
        assert _pick_main_plug([]) is None

    def test_a_script_still_wins_when_it_is_all_there_is(self):
        """A printer whose only entity is a script keeps the one-click run it has
        always had in that row."""
        script = _plug(1, plug_type="homeassistant", ha_entity_id="script.printer_on")

        assert _pick_main_plug([script]) is script


@pytest.mark.parametrize(
    ("field", "worse", "better"),
    [
        ("controls_printer_power", False, True),
        ("enabled", False, True),
        ("show_on_printer_card", False, True),
    ],
)
def test_each_flag_moves_the_rank_the_way_it_reads(field, worse, better):
    assert _main_plug_rank(_plug(1, **{field: better})) < _main_plug_rank(_plug(1, **{field: worse}))
