"""What the temperature route refuses, and the one thing it deliberately does not.

Registry N6. The gates are BS's own, read off ``StatusPanel``:

* the chamber field is read-only unless ``SupportChamberEdit()`` — a sensor is
  not a heater, and the X1C and P2S report a chamber temperature they cannot
  change;
* ``HasNozzleInstalled()`` refuses a nozzle setpoint with a modal;
* out-of-range input is refused by the widget, naming the bound it missed.

⚠️ **And no mid-print guard.** Every sibling control in this router asks for
``confirm`` while a print runs; this one must not. Adjusting a nozzle or a bed
mid-print is ordinary tuning — BS gates none of the three on print state — so a
guard copied from the fans would put an obstacle in front of the normal use.
That absence is load-bearing, which is why it is pinned here rather than left to
be "obviously" re-added later.

Pinned as source rather than exercised over HTTP because the route needs a live
MQTT client; the wire behaviour it guards is covered in
``test_temperature_control``.
"""

from __future__ import annotations

import inspect
import re

import pytest

from backend.app.api.routes import printers as printers_routes


def _bound(param: str, name: str) -> int | None:
    """One numeric constraint off a ``Query(...)`` annotation. They arrive as
    separate ``Ge``/``Le`` objects in ``metadata``, so the position of any one of
    them is not something to assert on."""
    meta = inspect.signature(printers_routes.set_temperature).parameters[param].default.metadata
    for entry in meta:
        if hasattr(entry, name):
            return getattr(entry, name)
    return None


def _source() -> str:
    """The route body with comments stripped — otherwise a prose mention of a
    guard reads as the guard itself."""
    src = inspect.getsource(printers_routes.set_temperature)
    return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))


class TestTheContract:
    @pytest.mark.parametrize("name", ["part", "target", "extruder_index"])
    def test_the_route_takes_it(self, name: str) -> None:
        assert name in inspect.signature(printers_routes.set_temperature).parameters

    def test_off_is_a_reachable_request(self) -> None:
        """⚠️ ``ge=0``, not ``ge=1``. Zero is how each heater is switched off,
        and a floor of 1 here would make "stop" unreachable through the API
        while the clamp below carefully preserves it."""
        assert _bound("target", "ge") == 0

    def test_the_deputy_is_the_highest_extruder_it_will_name(self) -> None:
        assert _bound("extruder_index", "ge") == 0
        assert _bound("extruder_index", "le") == 1


class TestTheGates:
    def test_an_unknown_part_is_refused_before_anything_is_sent(self) -> None:
        src = _source()
        assert re.search(r'part not in \("nozzle", "bed", "chamber"\)', src)

    def test_the_chamber_needs_a_heater_not_a_sensor(self) -> None:
        assert "supports_chamber_heater" in _source()

    def test_a_second_nozzle_is_refused_on_a_single_nozzle_machine(self) -> None:
        assert "_is_dual_nozzle" in _source()

    def test_only_an_explicit_false_refuses_for_a_missing_hotend(self) -> None:
        """⚠️ ``is False``, not falsy. An absent entry means the machine cannot
        detect a hotend at all — BS defaults ``m_has_nozzle`` to true for exactly
        that reason — so ``not client.state.ext_has_nozzle.get(...)`` would
        refuse every A- and P-series printer."""
        assert "ext_has_nozzle.get(extruder_index) is False" in _source()

    def test_the_range_is_checked_against_the_published_limits(self) -> None:
        """The same call the status snapshot publishes, so a client that bounded
        its input the way the status told it to cannot be refused here."""
        src = _source()
        assert "client.temperature_limits()" in src
        assert "is_within(target, limits)" in src


class TestThereIsNoMidPrintGuard:
    def test_the_route_does_not_ask_whether_the_printer_is_busy(self) -> None:
        """⚠️ Deliberate. See the module docstring — this is the one control in
        the router where a busy check would be the bug."""
        assert "is_printer_busy" not in _source()

    def test_and_takes_no_confirm_flag(self) -> None:
        assert "confirm" not in inspect.signature(printers_routes.set_temperature).parameters
