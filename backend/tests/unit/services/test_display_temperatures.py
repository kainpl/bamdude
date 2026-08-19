"""What the streaming overlay is allowed to see of a printer's temperatures.

``state.temperatures`` is not a readings dict — it is the MQTT client's working
memory. Derived flags (``nozzle_heating``) and private bookkeeping
(``_nozzle_target_set_time``) live in it alongside the numbers, and it grows
whenever that client learns something new.

⚠️ The overlay feed is authenticated by a **token**, which is a narrower grant
than a login: it exists so an OBS scene or a wall display can read one printer
without a session. Handing it the whole dict would mean it silently picks up
every field anybody adds to that dict later. So it gets an allow-list.

⚠️ And a reading the printer does not actually take must never reach a screen.
P1P, P1S, A1 and A1 mini all publish a ``chamber_temper`` with no sensor behind
it — the full status payload already drops it, and so does this.
"""

from __future__ import annotations

import pytest

from backend.app.services.printer_manager import DISPLAY_TEMPERATURE_KEYS, display_temperatures

pytestmark = pytest.mark.unit


class TestWhatItLetsThrough:
    def test_the_readings_the_overlay_draws(self):
        out = display_temperatures(
            {"nozzle": 219.6, "nozzle_target": 220, "bed": 60.0, "bed_target": 60},
            "X1C",
        )

        assert out == {"nozzle": 219.6, "nozzle_target": 220.0, "bed": 60.0, "bed_target": 60.0}

    def test_both_nozzles_on_a_dual_machine(self):
        out = display_temperatures({"nozzle": 220, "nozzle_2": 240}, "H2D")

        assert out["nozzle"] == 220.0
        assert out["nozzle_2"] == 240.0

    def test_values_come_back_as_floats(self):
        """The overlay rounds them; a string would render as "220°C" today and
        crash the moment anybody does arithmetic on it."""
        out = display_temperatures({"bed": "60"}, "X1C")

        assert out["bed"] == 60.0
        assert isinstance(out["bed"], float)

    def test_an_unparseable_value_is_dropped_rather_than_raised(self):
        out = display_temperatures({"bed": "warm", "nozzle": 220}, "X1C")

        assert out == {"nozzle": 220.0}

    def test_nothing_at_all(self):
        assert display_temperatures(None, "X1C") == {}
        assert display_temperatures({}, "X1C") == {}


class TestWhatItKeepsBack:
    def test_the_clients_own_bookkeeping(self):
        """⚠️ The whole reason this is an allow-list. These are real keys in
        that dict — a passthrough would put them on an OBS scene."""
        out = display_temperatures(
            {
                "nozzle": 220,
                "nozzle_heating": True,
                "_nozzle_target_set_time": 1_700_000_000.0,
                "bed_heating": False,
            },
            "X1C",
        )

        assert out == {"nozzle": 220.0}

    def test_a_field_nobody_has_added_yet(self):
        out = display_temperatures({"nozzle": 220, "something_new_next_release": 42}, "X1C")

        assert "something_new_next_release" not in out

    @pytest.mark.parametrize("model", ["P1P", "P1S", "A1", "A1 mini"])
    def test_a_chamber_reading_from_a_printer_with_no_chamber_sensor(self, model):
        """⚠️ These models publish a ``chamber_temper`` that means nothing. The
        overlay must never put a measurement on screen that does not exist."""
        out = display_temperatures({"nozzle": 220, "chamber": 31, "chamber_target": 0}, model)

        assert out == {"nozzle": 220.0}

    @pytest.mark.parametrize("model", ["X1C", "X1E", "H2D"])
    def test_but_keeps_it_where_the_sensor_is_real(self, model):
        out = display_temperatures({"chamber": 31, "chamber_target": 40}, model)

        assert out == {"chamber": 31.0, "chamber_target": 40.0}


def test_the_allow_list_is_readings_only():
    """A drift guard on the list itself: adding a flag here would hand it to
    every token holder, and nothing else would complain."""
    assert all(not key.startswith("_") for key in DISPLAY_TEMPERATURE_KEYS)
    assert not any(key.endswith("_heating") for key in DISPLAY_TEMPERATURE_KEYS)
    assert set(DISPLAY_TEMPERATURE_KEYS) == {
        "nozzle",
        "nozzle_target",
        "nozzle_2",
        "nozzle_2_target",
        "bed",
        "bed_target",
        "chamber",
        "chamber_target",
    }
